"""Hybrid search: every filter, their combinations, vector signals, RRF, saved searches, parsing."""

import re
from datetime import date

import numpy as np
import pytest

from backend.routers.search import parse_query
from tests.helpers.fixtures import FAKE_TEXT, FAKE_VISUAL, add_media, fake_vector


class FakeTextEncoder:
    """Maps any text containing a number N to media N's image vector (plus noise)."""

    spec = FAKE_TEXT

    def embed_texts(self, texts):
        out = []
        for text in texts:
            n = int(re.search(r"\d+", text).group()) if re.search(r"\d+", text) else 0
            v = fake_vector(n, FAKE_TEXT.dim) + 0.05 * fake_vector(n + 10_000, FAKE_TEXT.dim)
            out.append(v / np.linalg.norm(v))
        return np.stack(out)


def face_vector(person: int, i: int) -> np.ndarray:
    base = np.random.default_rng(person).standard_normal(512)
    noise = np.random.default_rng(10_000 + i).standard_normal(512) * 0.3
    v = (base + noise).astype(np.float32)
    return v / np.linalg.norm(v)


@pytest.fixture
def lib(app_services, monkeypatch):
    s = app_services
    ids = add_media(s.db, 120, kind="mixed")
    # People: Alice (1) on media 1-40, Bob (2) on 30-60; faces carry quality i/120.
    with s.db.connect() as conn:
        conn.execute("INSERT INTO people(id, name, face_count) VALUES (1,'Alice',40),(2,'Bob Stone',31)")
    faces = []
    for mid in ids:
        owners = [p for p, lo, hi in ((1, 1, 40), (2, 30, 60)) if lo <= mid <= hi]
        for p in owners:
            offset, sha = s.store.append(face_vector(p, mid))
            faces.append((mid, p, offset, sha, mid / 120))
    with s.db.connect() as conn:
        conn.executemany("INSERT INTO faces(media_id, person_id, bbox, detection, embedding_offset, embedding_sha, quality)"
                         " VALUES (?,?,'[0,0,10,10]',0.9,?,?,?)", faces)
    text = s.vectors.register(FAKE_TEXT)
    visual = s.vectors.register(FAKE_VISUAL)
    text.add([(i, fake_vector(i, FAKE_TEXT.dim)) for i in ids])
    visual.add([(i, fake_vector(i, FAKE_VISUAL.dim, salt=5)) for i in ids])
    monkeypatch.setattr(s.models, "text_encoder", lambda: FakeTextEncoder())
    return s


def ids_of(result):
    return [item["id"] for item in result["items"]]


def run(s, **q):
    q.setdefault("limit", 200)
    return s.search.run(q)


def media(s, where="1=1", params=()):
    return {r["id"] for r in s.db.all(f"SELECT id FROM media WHERE {where}", params)}


def test_filter_only_kind_and_dates(lib):
    assert set(ids_of(run(lib, kind="video"))) == media(lib, "kind='video'")
    r = run(lib, date_from="2020-02-01", date_to="2020-02-28")
    assert set(ids_of(r)) == media(lib, "captured_at BETWEEN '2020-02-01' AND '2020-02-28T23:59:59'")
    assert r["signals"] == ["recency"]
    dates = [i["captured_at"] for i in r["items"]]
    assert dates == sorted(dates, reverse=True)


def test_people_any_all_exclude_quality(lib):
    assert set(ids_of(run(lib, people=[1]))) == set(range(1, 41))
    assert set(ids_of(run(lib, people=[1, 2]))) == set(range(1, 61))
    assert set(ids_of(run(lib, people=[1, 2], people_mode="ALL"))) == set(range(30, 41))
    assert set(ids_of(run(lib, exclude_people=[2]))) == set(range(1, 121)) - set(range(30, 61))
    assert set(ids_of(run(lib, min_quality=0.4))) == {m for m in range(1, 61) if m / 120 >= 0.4}


def test_filter_combinations(lib):
    r = run(lib, people=[2], kind="photo", min_quality=0.3, exclude_people=[1])
    assert set(ids_of(r)) == {m for m in range(41, 61) if m % 5 and m / 120 >= 0.3}
    # Rejected faces and exclusions do not count as the person being present.
    with lib.db.connect() as conn:
        conn.execute("UPDATE faces SET review_state='rejected' WHERE media_id=35 AND person_id=2")
        conn.execute("INSERT INTO exclusions(person_id, media_id) VALUES (2, 36)")
    assert {35, 36}.isdisjoint(ids_of(run(lib, people=[2], people_mode="ALL")))


def test_text_signal_ranks_target_first_and_respects_filters(lib):
    r = run(lib, text="the photo number 42")
    assert ids_of(r)[0] == 42 and r["signals"] == ["text"]
    assert r["items"][0]["signals"]["text"]["rank"] == 1
    filtered = run(lib, text="photo 42", people=[1])  # 42 has neither person
    assert 42 not in ids_of(filtered) and set(ids_of(filtered)) <= set(range(1, 41))
    assert ids_of(run(lib, text="photo 17", people=[1]))[0] == 17


def test_similar_media_and_face_signals(lib):
    r = run(lib, similar_media_id=10)
    assert 10 not in ids_of(r) and r["signals"] == ["similar_media"]
    by_face = run(lib, similar_face_id=lib.db.one("SELECT id FROM faces WHERE media_id=5")["id"], limit=40)
    top = ids_of(by_face)
    assert set(top[:35]) <= set(range(1, 41)), top  # Alice's media first
    lib.search._face_sync.join(30)  # brute force meanwhile, ANN afterwards
    assert set(ids_of(run(lib, similar_face_id=1, limit=30))) <= set(range(1, 41))


def test_rrf_fuses_and_weights_disable_signals(lib):
    both = run(lib, text="img 42", similar_media_id=42, weights={"text": 1, "similar_media": 1})
    assert both["signals"] == ["similar_media", "text"]
    # Items found by both signals outrank items found by one; 42 (text only, rank 1) stays near the top.
    assert set(both["items"][0]["signals"]) == {"text", "similar_media"}
    for item in both["items"]:
        expected = sum(1 / (60 + d["rank"]) for d in item["signals"].values())
        assert item["score"] == pytest.approx(expected, abs=1e-6)
    scores = [i["score"] for i in both["items"]]
    assert scores == sorted(scores, reverse=True)
    text_off = run(lib, text="img 42", similar_media_id=42, weights={"text": 0})
    assert 42 not in ids_of(text_off)
    with_recency = run(lib, text="img 42", weights={"recency": 0.5})
    assert "recency" in with_recency["signals"]
    newest = max(with_recency["items"], key=lambda i: (i["captured_at"], i["id"]))
    assert newest["signals"]["recency"]["rank"] == 1


def test_pagination_is_stable(lib):
    full = ids_of(run(lib, text="img 7", limit=200))
    pages = ids_of(lib.search.run({"text": "img 7", "limit": 25, "page": 1})) + \
        ids_of(lib.search.run({"text": "img 7", "limit": 25, "page": 2}))
    assert pages == full[:50]


def test_missing_models_degrade_to_filters_with_warning(app_services):
    add_media(app_services.db, 5)
    r = app_services.search.run({"text": "beach", "kind": "photo"})
    assert r["total"] == 5 and "fetch_models" in r["warnings"][0]


def test_hybrid_endpoint_and_saved_searches(client, lib):
    r = client.post("/api/search/hybrid", json={"text": "img 42", "limit": 5})
    assert r.status_code == 200 and r.json()["items"][0]["id"] == 42
    assert client.post("/api/search/hybrid", json={"kind": "gif"}).status_code == 422
    saved = client.post("/api/search/saved", json={"name": "Alice photos", "query": {"people": [1], "kind": "photo"}}).json()
    assert saved["query"] == {"people": [1], "people_mode": "ANY", "exclude_people": [], "kind": "photo",
                              "deleted": False, "weights": {}}
    assert [s["name"] for s in client.get("/api/search/saved").json()["items"]] == ["Alice photos"]
    ran = client.post(f"/api/search/saved/{saved['id']}/run", json={"limit": 100}).json()
    assert set(i["id"] for i in ran["items"]) == {m for m in range(1, 41) if m % 5}
    assert ran["saved_search"]["run_count"] == 1
    # Re-runnable: new matching media shows up on the next run.
    with lib.db.connect() as conn:
        conn.execute("INSERT INTO faces(media_id, person_id, bbox, detection, embedding_offset, embedding_sha)"
                     " VALUES (41, 1, '[0,0,1,1]', 0.9, 0, 'x')")
    assert 41 in [i["id"] for i in client.post(f"/api/search/saved/{saved['id']}/run").json()["items"]]
    assert client.delete(f"/api/search/saved/{saved['id']}").json() == {"ok": True}
    assert client.delete(f"/api/search/saved/{saved['id']}").status_code == 404


def test_parse_keeps_contract_and_adds_structure(client, lib):
    r = client.post("/api/search/parse", json={"query": "Bob Stone and alice beach photos in June 2021"}).json()
    assert r["people"] == [2, 1] and r["mode"] == "ALL" and r["kind"] == "photo"
    assert (r["date_from"], r["date_to"]) == ("2021-06-01", "2021-06-30")
    assert r["unmatched"] == ["beach"] and r["text"] == "beach"
    assert r["filters"]["people_mode"] == "ALL"
    assert r["embedding_query"] == {"text": "beach", "model": FAKE_TEXT.key, "available": True}
    vec = client.post("/api/search/parse", json={"query": "img 3", "embed": True}).json()["embedding_query"]["vector"]
    assert len(vec) == FAKE_TEXT.dim


@pytest.mark.parametrize("query,expected", [
    ("2019", ("2019-01-01", "2019-12-31")),
    ("since 2018", ("2018-01-01", None)),
    ("before 2018", (None, "2017-12-31")),
    ("last year", ("2025-01-01", "2025-12-31")),
    ("this month", ("2026-09-01", "2026-09-30")),
    ("december", ("2025-12-01", "2025-12-31")),
    ("yesterday", ("2026-09-29", "2026-09-29")),
])
def test_date_phrases(query, expected, app_services):
    r = parse_query(query, today=date(2026, 9, 30))
    assert (r["date_from"], r["date_to"]) == expected and r["text"] == ""
