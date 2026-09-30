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


def test_lqip_placeholders_from_cached_thumbnails(client, app_services):
    from PIL import Image

    from tests.helpers.fixtures import add_media
    ids = add_media(app_services.db, 2)
    Image.new("RGB", (480, 320), (10, 120, 200)).save(app_services.config.data_dir / "thumbnails" / f"media-{ids[0]}.jpg")
    items = client.get(f"/api/media/lqip?ids={ids[0]},{ids[1]},x").json()["items"]
    assert list(items) == [str(ids[0])] and items[str(ids[0])].startswith("data:image/jpeg;base64,")
    assert len(items[str(ids[0])]) < 1200


def test_review_decisions_can_be_undone(client, app_services):
    from tests.helpers.fixtures import add_media
    add_media(app_services.db, 1)
    with app_services.db.connect() as conn:
        conn.execute("INSERT INTO people(id, name, face_count) VALUES (1, 'A', 1)")
        conn.execute("INSERT INTO faces(id, media_id, person_id, bbox, detection, embedding_offset, embedding_sha)"
                     " VALUES (1, 1, 1, '[0,0,1,1]', 0.9, 0, 'x')")
    assert client.post("/api/faces/1/review", json={"decision": "no"}).json() == {"ok": True}
    assert app_services.db.one("SELECT COUNT(*) c FROM rejections")["c"] == 1
    assert client.post("/api/faces/1/review", json={"decision": "reset"}).json() == {"ok": True}
    face = app_services.db.one("SELECT review_state, person_id FROM faces WHERE id=1")
    assert face == {"review_state": "unreviewed", "person_id": 1}
    assert app_services.db.one("SELECT COUNT(*) c FROM rejections")["c"] == 0
    assert app_services.db.one("SELECT COUNT(*) c FROM hard_negatives")["c"] == 0
    assert client.post("/api/faces/1/review", json={"decision": "maybe"}).status_code == 400
