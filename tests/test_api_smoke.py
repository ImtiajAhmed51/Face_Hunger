"""Behavioural smoke tests over the refactored routers (no AI models needed)."""


def test_dashboard_and_lists_on_empty_library(client):
    d = client.get("/api/dashboard").json()
    assert d["photos"] == 0 and d["people"] == 0 and d["job"] is None
    assert client.get("/api/people").json() == {"items": [], "total": 0, "page": 1, "limit": 48}
    assert client.get("/api/media?kind=photo").json()["total"] == 0
    assert client.get("/api/review").json()["total"] == 0
    assert client.get("/api/libraries").json() == {"items": []}


def test_csrf_header_is_required(client):
    r = client.post("/api/search/parse", json={"query": "x"}, headers={"X-LFS-Request": ""})
    assert r.status_code == 403


def test_settings_roundtrip(client):
    s = client.get("/api/settings").json()
    assert s["matching_threshold"] == 0.48
    r = client.patch("/api/settings", json={"review_threshold": 0.7})
    assert r.status_code == 200 and r.json()["review_threshold"] == 0.7
    assert client.patch("/api/settings", json={"theme": "neon"}).status_code == 400


def test_library_registration_and_spa(client, tmp_path):
    lib = tmp_path / "photos"
    lib.mkdir()
    r = client.post("/api/libraries", json={"path": str(lib)})
    assert r.status_code == 200, r.text
    assert client.get("/api/libraries").json()["items"][0]["path"] == str(lib.resolve())
    assert client.get("/some/deep/link").status_code == 200
    assert client.get("/api/nope").status_code == 404


def test_maintenance_index_reset_swaps_store(client, app_services):
    before = app_services.store
    r = client.post("/api/maintenance", json={"action": "index", "confirm": "CLEAR AI INDEX"})
    assert r.json() == {"ok": True, "cleared": "index"}
    assert app_services.store is not before
    assert app_services.worker.store is app_services.store


def test_model_and_embedding_status_without_models(client):
    status = client.get("/api/models").json()
    assert {m["key"] for m in status["models"]} >= {"siglip2-base-patch16-224@1:768", "dinov2-small@1:384"}
    assert all(not m["installed"] for m in status["models"])
    keys = {i["key"] for i in client.get("/api/embeddings").json()["items"]}
    assert "buffalo_l-w600k_r50@1:512" in keys and "dinov2-vitb14-torchhub@1:768" in keys


def test_duplicates_endpoints_work_without_dino_model(client):
    d = client.get("/api/duplicates").json()
    assert d["groups"] == [] and d["dino"]["available"] is False
    b = client.post("/api/duplicates/backfill", json={"limit": 5}).json()
    assert b["filled"] == 0 and "fetch_models" in b["errors"][0]
