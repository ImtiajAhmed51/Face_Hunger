"""Diagnostics: off means no work and no writes; on stays local; the report carries no paths or names."""

import json
import socket
from pathlib import Path

import pytest

from backend.ops import diagnostics
from backend.routers.diagnostics import build_report
from tests.helpers.fixtures import FAKE_TEXT, FAKE_VISUAL, FakeEmbedder, add_media, fake_vector
from tests.test_search import FakeTextEncoder

PERSON = "Zelda Fitzgerald"


def snapshot(root: Path) -> dict:
    return {str(p.relative_to(root)): p.stat().st_mtime_ns for p in root.rglob("*") if p.is_file()}


@pytest.fixture
def busy(app_services, tmp_path, monkeypatch):
    """A library with a named person, a named album, and a job that fails with a path in its message."""
    from backend.services.container import set_current

    s = app_services
    set_current(s)
    lib = tmp_path / "Private Holiday Pictures"
    lib.mkdir()
    ids = add_media(s.db, 60, library_path=str(lib))
    with s.db.connect() as conn:
        conn.execute("INSERT INTO people(id, name, face_count) VALUES (1, ?, 3)", (PERSON,))
        conn.execute("INSERT INTO albums(id, name) VALUES (1, 'Secret Trip To Lisbon')")
    s.vectors.register(FAKE_TEXT).add([(i, fake_vector(i, FAKE_TEXT.dim)) for i in ids])
    s.extra_embedders[FAKE_VISUAL.key] = FakeEmbedder(FAKE_VISUAL)
    monkeypatch.setattr(s.models, "text_encoder", lambda: FakeTextEncoder())

    def explode(ctx):
        raise RuntimeError(f"cannot read {lib}/img_7.jpg of {PERSON}")

    s.jobs.register("explode", explode)

    def work():
        s.jobs.start()
        done = s.jobs.wait(s.jobs.enqueue("embed_backfill", {"key": FAKE_VISUAL.key})["id"], timeout=60)
        assert done["status"] == "completed", done
        assert s.jobs.wait(s.jobs.enqueue("explode", {"path": str(lib)})["id"], timeout=60)["status"] == "failed"
        for query in (f"{PERSON} 7", "photo 12", f"{lib}/img_3.jpg"):
            assert s.search.run({"text": query, "limit": 10})["items"]
        diagnostics.count("thumbnail", True)
        diagnostics.count("thumbnail", False)
        diagnostics.record("model_load", f"{lib}/model.onnx", 1234.5, path=str(lib), size=3)
        diagnostics.record("render", "route.people", 41.0)

    return s, lib, work


def test_off_by_default_no_overhead_path_and_zero_writes(busy):
    s, _lib, work = busy
    assert diagnostics.enabled() is False and s.db.settings().get("diagnostics_enabled") is None
    # The off path hands back one shared object: nothing is timed, allocated or stored.
    assert diagnostics.span("search", "hybrid") is diagnostics.NOOP
    assert diagnostics.span("job", "x", items=3) is diagnostics.span("embed", "y")
    work()
    assert not s.diagnostics.path.exists() and not list(Path(s.config.data_dir).glob("diagnostics*"))
    assert s.diagnostics._events == [] and s.diagnostics._counts == {}
    summary = s.diagnostics.summary()
    assert summary["enabled"] is False and summary["events"] == 0
    assert not s.diagnostics.path.exists()  # reading the (empty) summary does not create the file either


def test_on_records_locally_and_opens_no_sockets(busy, monkeypatch):
    s, _lib, work = busy
    opened = []
    real_socket = socket.socket

    class Watched(real_socket):
        def __init__(self, *args, **kwargs):
            opened.append(args)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(socket, "socket", Watched)
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: opened.append(a))
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: opened.append(a))
    data_dir = Path(s.config.data_dir)
    before = set(snapshot(data_dir.parent))
    s.diagnostics.set_enabled(True)
    work()
    summary = s.diagnostics.summary()
    report = build_report()
    s.diagnostics.set_enabled(False)
    assert opened == []
    # Everything it wrote is its own file inside the data folder.
    new_files = set(snapshot(data_dir.parent)) - before
    assert {f for f in new_files if "diagnostics" in f} and all(f.startswith(data_dir.name + "/") for f in new_files)
    kinds = {(o["kind"], o["name"]) for o in summary["operations"]}
    assert {("job", "embed_backfill"), ("job", "explode"), ("search", "hybrid"), ("embed", "fake-visual@1:32"),
            ("render", "route.people")} <= kinds
    search = next(o for o in summary["operations"] if o["kind"] == "search")
    assert search["count"] == 3 and search["p95_ms"] >= search["p50_ms"] > 0
    assert summary["caches"] == [{"name": "thumbnail", "hits": 1, "misses": 1, "hit_rate": 0.5}]
    assert summary["model_loads"][0]["ms"] == 1234.5
    assert any(e["kind"] == "job" and e.get("failed") == 1 and e["peak_rss_mb"] > 0 for e in summary["slow"])
    assert report["events"] == summary["events"]
    # Off again: recording stops immediately.
    count = s.diagnostics.summary()["events"]
    s.search.run({"text": "photo 3", "limit": 5})
    assert s.diagnostics.summary()["events"] == count


def test_exported_report_has_no_paths_or_names(busy, client, tmp_path):
    s, lib, work = busy
    assert client.patch("/api/diagnostics", json={"enabled": True}).json()["enabled"] is True
    assert s.db.settings()["diagnostics_enabled"] is True
    work()
    for path in ("/api/media?limit=5", "/api/media/7", "/api/people/1", "/api/media/999999"):
        client.get(path)
    assert client.post("/api/diagnostics/client", json={"name": "route.photos", "ms": 18.5}).status_code == 200
    assert client.post("/api/diagnostics/client", json={"name": f"{lib}/x", "ms": 1}).status_code == 422
    response = client.get("/api/diagnostics/report")
    assert response.status_code == 200 and "attachment" in response.headers["content-disposition"]
    text = response.text
    report = json.loads(text)
    assert report["events"] > 8 and report["library"]["items"] == 60
    routes = {o["name"] for o in report["operations"] if o["kind"] == "request"}
    assert "GET .api.media.{media_id}" in routes and not any("999999" in r or "/7" in r for r in routes)
    forbidden = [str(lib), lib.name, str(tmp_path), str(Path.home()), PERSON, "Zelda", "Fitzgerald", "Lisbon", "Secret Trip",
                 "img_7", "img_3", ".jpg", "model.onnx", str(s.config.data_dir)]
    for needle in forbidden:
        assert needle not in text, needle
    assert "/Users/" not in text and "/private/" not in text and "\\\\" not in text
    # The raw store is just as clean: names are sanitised when recorded, not only when exported.
    raw = s.diagnostics.path.read_bytes()
    for needle in (PERSON.encode(), lib.name.encode(), b"img_7", b"Lisbon"):
        assert needle not in raw
    # The scrubber itself, on a hostile summary.
    dirty = s.diagnostics.report(secrets=[PERSON, str(lib)], library={"note": f"seen {PERSON} at {lib}/a.jpg", "other": "C:\\Users\\me\\x.png", "ok": "fine"})
    assert dirty["library"] == {"note": "[redacted]", "other": "[redacted]", "ok": "fine"}
    # Clear deletes the file; turning it off persists.
    assert client.delete("/api/diagnostics").json()["events"] <= 1
    assert client.patch("/api/diagnostics", json={"enabled": False}).json()["enabled"] is False
    assert diagnostics.enabled() is False and client.post("/api/diagnostics/client", json={"name": "route.x", "ms": 1}).status_code == 409


def test_setting_survives_restart(make_config):
    from backend.services.container import Services
    from tests.conftest import FakeEngine

    config = make_config()
    first = Services(config, engine=FakeEngine())
    with first.db.connect() as conn:
        conn.execute("INSERT INTO settings(key, value) VALUES ('diagnostics_enabled', 'true')")
    first.close()
    second = Services(config, engine=FakeEngine())
    try:
        assert diagnostics.enabled() and second.diagnostics.on
    finally:
        second.close()
    assert not diagnostics.enabled()
