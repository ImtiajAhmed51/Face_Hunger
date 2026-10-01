"""Albums, favorites, smart collections, merge/split round trips, audit log + undo, memories."""

import shutil
import time
from datetime import date

import numpy as np
import pytest
from PIL import Image

from tests.helpers.fixtures import add_media

TABLES = ("albums", "album_media", "favorites", "people", "faces", "exclusions", "rejections", "hard_negatives")


def snapshot(db):
    def norm(row):
        return {k: v for k, v in row.items() if k not in ("updated_at", "centroid", "face_count", "representative_face_id",
                                                          "variance", "added_at", "created_at")}
    return {t: sorted((tuple(sorted(norm(r).items())) for r in db.all(f"SELECT * FROM {t}")), key=repr) for t in TABLES} | {
        "deleted": [r["id"] for r in db.all("SELECT id FROM media WHERE deleted_at IS NOT NULL ORDER BY id")]}


def unit(seed, noise=0.0, salt=0):
    v = np.random.default_rng(seed).standard_normal(512) + noise * np.random.default_rng(salt + 999).standard_normal(512)
    return (v / np.linalg.norm(v)).astype(np.float32)


@pytest.fixture
def people_lib(app_services):
    """Alice (id 1): faces on media 1-3; Bob (id 2): faces on media 4-5; exclusions/rejections/hard negatives."""
    s = app_services
    add_media(s.db, 8)
    with s.db.connect() as conn:
        conn.execute("INSERT INTO people(id, name) VALUES (1, 'Alice'), (2, 'Bob')")
        for media_id, person in ((1, 1), (2, 1), (3, 1), (4, 2), (5, 2)):
            off, sha = s.store.append(unit(person, 0.3, media_id))
            conn.execute("INSERT INTO faces(id, media_id, person_id, bbox, detection, embedding_offset, embedding_sha, quality) "
                         "VALUES (?,?,?, '[0,0,9,9]', 0.9, ?, ?, 0.8)", (media_id * 10, media_id, person, off, sha))
        conn.execute("INSERT INTO exclusions(person_id, media_id) VALUES (1, 6), (2, 6), (2, 7)")
        conn.execute("INSERT INTO rejections(face_id, person_id) VALUES (40, 1), (10, 2)")
        conn.execute("INSERT INTO hard_negatives(face_id, person_id, similarity) VALUES (40, 1, 0.3)")
    return s


def test_merge_then_split_round_trips_the_exact_faces(client, people_lib):
    s = people_lib
    preview = client.get("/api/people/1/merge-preview?target_id=2").json()
    assert preview["confidence"] == "low" and preview["face_count"] == 3  # different random identities
    merged = client.post("/api/people/1/merge", json={"target_id": 2}).json()
    assert merged["id"] == 2 and merged["audit_id"]
    assert {r["id"] for r in s.db.all("SELECT id FROM faces WHERE person_id=2")} == {10, 20, 30, 40, 50}
    split = client.post("/api/people/2/split", json={"face_ids": [10, 20, 30], "name": "Alice"}).json()
    assert {r["id"] for r in s.db.all("SELECT id FROM faces WHERE person_id=?", (split["person_id"],))} == {10, 20, 30}
    assert {r["id"] for r in s.db.all("SELECT id FROM faces WHERE person_id=2")} == {40, 50}
    assert client.post("/api/people/2/split", json={"face_ids": [10]}).status_code == 400  # 10 is not Bob's now


def test_merge_undo_restores_everything_exactly(client, people_lib):
    s = people_lib
    before = snapshot(s.db)
    merged = client.post("/api/people/1/merge", json={"target_id": 2}).json()
    assert snapshot(s.db) != before
    assert client.post(f"/api/audit/{merged['audit_id']}/undo").json()["ok"]
    assert snapshot(s.db) == before
    assert client.post(f"/api/audit/{merged['audit_id']}/undo").status_code == 409  # only once


def test_every_batch_action_is_logged_and_undoable(client, people_lib):
    s = people_lib
    actions = [
        lambda: client.post("/api/albums", json={"name": "Trip", "media_ids": [1, 2, 3]}),
        lambda: client.post("/api/albums/1/items", json={"media_ids": [3, 4, 5]}),
        lambda: client.post("/api/albums/1/items/remove", json={"media_ids": [1, 4]}),
        lambda: client.patch("/api/albums/1", json={"name": "Big trip"}),
        lambda: client.post("/api/favorites", json={"media_ids": [2, 3, 6]}),
        lambda: client.post("/api/favorites", json={"media_ids": [3], "favorite": False}),
        lambda: client.post("/api/batch/delete", json={"media_ids": [6, 7]}),
        lambda: client.post("/api/batch/restore", json={"media_ids": [7]}),
        lambda: client.post("/api/faces/move", json={"face_ids": [40], "target_id": 1}),
        lambda: client.post("/api/people/1/split", json={"face_ids": [10, 20]}),
        lambda: client.post("/api/people/1/merge", json={"target_id": 2}),
        lambda: client.delete("/api/albums/1"),
    ]
    states = [snapshot(s.db)]
    for act in actions:
        r = act()
        assert r.status_code == 200, r.text
        states.append(snapshot(s.db))
    log = client.get("/api/audit").json()["items"]
    assert len(log) == len(actions) and all(e["undoable"] for e in log)
    assert log[0]["action"] == "album.delete" and log[-1]["action"] == "album.create"
    # Undo newest-first; after each undo the state equals the state before that action.
    for entry, expected in zip(log, reversed(states[:-1])):
        assert client.post(f"/api/audit/{entry['id']}/undo").status_code == 200, entry
        assert snapshot(s.db) == expected, entry["action"]
    assert client.get("/api/audit?limit=500").json()["items"][0]["action"] == "undo"


def test_album_and_favorite_listing(client, people_lib):
    client.post("/api/albums", json={"name": "Picks", "media_ids": [5, 2, 8]})
    items = client.get("/api/media?album=1&limit=10").json()["items"]
    assert [i["id"] for i in items] == [5, 2, 8]  # album order
    client.post("/api/favorites", json={"media_ids": [2]})
    favs = client.get("/api/media?favorite=true").json()["items"]
    assert [i["id"] for i in favs] == [2] and favs[0]["favorite"] is True
    albums = client.get("/api/albums").json()["items"]
    assert albums[0]["item_count"] == 3 and albums[0]["cover_media_id"] == 5


def test_smart_collection_updates_when_a_matching_file_is_watched_in(client, app_services, tmp_path):
    library = tmp_path / "library"
    library.mkdir()
    client.post("/api/libraries", json={"path": str(library)})
    created = client.post("/api/collections", json={"name": "Beach", "query": {"name": "beach", "kind": "photo"}}).json()
    assert client.get(f"/api/collections/{created['id']}/items").json()["total"] == 0
    source = tmp_path / "src.jpg"
    Image.new("RGB", (64, 48), (20, 120, 200)).save(source)
    time.sleep(0.3)
    shutil.copy(source, library / "beach_day.jpg")
    shutil.copy(source, library / "office.jpg")
    deadline = time.monotonic() + 8
    names: list = []
    while time.monotonic() < deadline:
        names = [i["name"] for i in client.get(f"/api/collections/{created['id']}/items").json()["items"]]
        if names:
            break
        time.sleep(0.1)
    assert names == ["beach_day.jpg"]
    listed = client.get("/api/collections").json()["items"]
    assert listed[0]["item_count"] == 1 and listed[0]["name"] == "Beach"
    # Collections are not mixed into the saved-search list.
    assert client.get("/api/search/saved").json()["items"][0]["name"] == "Beach"


def test_memories_on_this_day(client, app_services):
    s = app_services
    ids = add_media(s.db, 4)
    today = date.today()
    with s.db.connect() as conn:
        conn.execute("UPDATE media SET captured_at=?, date_source='exif' WHERE id IN (?, ?)",
                     (f"{today.year - 3}-{today:%m-%d}T10:00:00", ids[0], ids[1]))
        conn.execute("UPDATE media SET captured_at=?, date_source='mtime' WHERE id=?", (f"{today.year - 1}-{today:%m-%d}T10:00:00", ids[2]))
    sections = client.get("/api/memories").json()["sections"]
    assert [(x["kind"], x["title"], sorted(x["media_ids"])) for x in sections] == [("on_this_day", "3 years ago today", sorted(ids[:2]))]
