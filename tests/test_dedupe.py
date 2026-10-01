"""Duplicate resolution: suggestions, bursts, resolve frees the predicted bytes, undo is byte-identical."""

import hashlib
import json
import os
import shutil

import numpy as np
import pytest
from PIL import Image

from backend.duplicates import content_hash


def tree_state(root):
    """{relative path: sha256} for every file under root."""
    out = {}
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            p = os.path.join(dirpath, name)
            out[os.path.relpath(p, root)] = hashlib.sha256(open(p, "rb").read()).hexdigest()
    return out


def folder_bytes(root):
    return sum(os.path.getsize(os.path.join(d, f)) for d, _s, fs in os.walk(root) for f in fs)


@pytest.fixture
def twenty_groups(app_services, tmp_path):
    """20 groups: an original plus 1-2 byte-identical copies in another folder (exact duplicates)."""
    s = app_services
    lib = tmp_path / "library"
    (lib / "camera").mkdir(parents=True)
    (lib / "backup").mkdir()
    rng = np.random.default_rng(4)
    with s.db.connect() as conn:
        conn.execute("INSERT INTO libraries(id, path, name) VALUES (1, ?, 'lib')", (str(lib),))
    rows = []
    for g in range(20):
        img = Image.fromarray(rng.integers(0, 255, (120 + g, 160, 3), dtype=np.uint8))
        original = lib / "camera" / f"IMG_{g:04d}.jpg"
        img.save(original, quality=90)
        copies = [lib / "backup" / f"IMG_{g:04d}.jpg"] + ([lib / "backup" / f"IMG_{g:04d} (1).jpg"] if g % 3 == 0 else [])
        for c in copies:
            shutil.copy(original, c)
        for path in [original, *copies]:
            rows.append((str(path), path.name, path.stat().st_size, content_hash(path), 160, 120 + g))
    with s.db.connect() as conn:
        conn.executemany("INSERT INTO media(library_id, path, name, kind, size, mtime_ns, content_hash, width, height, status)"
                         " VALUES (1, ?, ?, 'photo', ?, 1, ?, ?, ?, 'indexed')", rows)
    return s, lib


def test_twenty_groups_free_predicted_bytes_and_undo_is_byte_identical(client, twenty_groups):
    s, lib = twenty_groups
    before_tree = tree_state(lib)
    before_bytes = folder_bytes(lib)
    before_rows = s.db.all("SELECT id, path, deleted_at FROM media ORDER BY id")
    listing = client.get("/api/duplicates").json()
    groups = [g for g in listing["groups"] if g["type"] == "exact"]
    assert len(groups) == 20
    predicted = sum(g["suggestion"]["savings_bytes"] for g in groups)
    assert listing["savings_bytes"] == predicted > 0
    decisions = [{"keep": g["suggestion"]["keep"], "remove": g["suggestion"]["remove"]} for g in groups]
    result = client.post("/api/duplicates/resolve", json={"groups": decisions, "free_space": True}).json()
    assert result["removed"] == 27 and result["predicted_bytes"] == predicted
    assert before_bytes - folder_bytes(lib) == predicted == result["moved_bytes"]  # exactly the predicted bytes
    assert client.get("/api/duplicates/bin").json()["bytes"] == predicted
    assert s.db.one("SELECT COUNT(*) c FROM media WHERE deleted_at IS NOT NULL")["c"] == 27
    assert not [g for g in client.get("/api/duplicates").json()["groups"] if g["type"] == "exact"]

    undo = client.post(f"/api/audit/{result['audit_id']}/undo")
    assert undo.status_code == 200, undo.text
    assert tree_state(lib) == before_tree  # every file back, byte-identical
    assert s.db.all("SELECT id, path, deleted_at FROM media ORDER BY id") == before_rows
    assert client.get("/api/duplicates/bin").json() == {"bytes": 0, "files": 0}
    assert client.post(f"/api/audit/{result['audit_id']}/undo").status_code == 409


def test_keep_best_uses_best_shot_score_and_soft_delete_only_by_default(client, twenty_groups):
    s, lib = twenty_groups
    group = next(g for g in client.get("/api/duplicates").json()["groups"] if len(g["items"]) == 3)
    ids = [i["id"] for i in group["items"]]
    with s.db.connect() as conn:  # make the second copy the best shot
        conn.executemany("INSERT INTO quality_scores VALUES (?, 1, ?, '{}')", [(ids[0], 0.4), (ids[1], 0.9), (ids[2], 0.5)])
    group = next(g for g in client.get("/api/duplicates").json()["groups"] if g["key"] == group["key"])
    assert group["suggestion"]["keep"] == [ids[1]] and group["suggestion"]["reason"] == "best-shot score"
    before = tree_state(lib)
    r = client.post("/api/duplicates/resolve", json={"groups": [group["suggestion"]]}).json()
    assert r["moved"] == 0 and tree_state(lib) == before  # database-only by default
    assert client.post("/api/duplicates/resolve", json={"groups": [{"keep": [1], "remove": [1]}]}).status_code == 400


def test_failed_move_rolls_everything_back(client, twenty_groups, monkeypatch):
    s, lib = twenty_groups
    before = tree_state(lib)
    groups = [g["suggestion"] for g in client.get("/api/duplicates").json()["groups"]]
    real_move = shutil.move
    calls = {"n": 0}

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] == 5:
            raise OSError("disk full")
        return real_move(src, dst)

    monkeypatch.setattr(shutil, "move", flaky)
    with pytest.raises(OSError):
        s.dedupe.resolve(groups, free_space=True)
    monkeypatch.setattr(shutil, "move", real_move)
    assert tree_state(lib) == before
    assert s.db.one("SELECT COUNT(*) c FROM media WHERE deleted_at IS NOT NULL")["c"] == 0


def test_empty_bin_requires_confirmation_and_blocks_undo(client, twenty_groups):
    s, _ = twenty_groups
    groups = [g["suggestion"] for g in client.get("/api/duplicates").json()["groups"]][:2]
    r = client.post("/api/duplicates/resolve", json={"groups": groups, "free_space": True}).json()
    assert client.post("/api/duplicates/bin/empty", json={"confirm": "yes"}).status_code == 400
    emptied = client.post("/api/duplicates/bin/empty", json={"confirm": "EMPTY"}).json()
    assert emptied["bytes"] == r["moved_bytes"] and emptied["groups"] == 1
    assert client.post(f"/api/audit/{r['audit_id']}/undo").status_code == 409


def test_bursts_group_rapid_similar_shots(client, app_services):
    s = app_services
    with s.db.connect() as conn:
        conn.execute("INSERT INTO libraries(id, path, name) VALUES (1, '/l', 'l')")
        shots = [("2024-05-01T10:00:00", "aaaa0000aaaa0000"), ("2024-05-01T10:00:01", "aaaa0000aaaa0001"),
                 ("2024-05-01T10:00:02", "aaaa0000aaaa0003"),  # burst of 3
                 ("2024-05-01T10:00:30", "aaaa0000aaaa0000"),  # 28 s later: separate
                 ("2024-05-01T12:00:00", "0000ffff0000ffff"), ("2024-05-01T12:00:01", "ffff0000ffff0000")]  # not alike
        for i, (when, phash) in enumerate(shots, start=1):
            conn.execute("INSERT INTO media(id, library_id, path, name, kind, size, mtime_ns, captured_at, date_source, phash,"
                         " camera_make, camera_model, status, width, height) VALUES (?,1,?,?,'photo',?,1,?,'exif',?,'X','Y','indexed',10,10)",
                         (i, f"/l/{i}.jpg", f"{i}.jpg", 1000 * i, when, phash))
    groups = client.get("/api/duplicates/bursts").json()["groups"]
    assert [[i["id"] for i in g["items"]] for g in groups] == [[1, 2, 3]]
    assert groups[0]["suggestion"]["keep"] == [3]  # no scores yet: largest wins
    client.post("/api/duplicates/ignore", json={"media_ids": [1, 2, 3]})
    assert client.get("/api/duplicates/bursts").json()["groups"] == []
    assert json.loads(json.dumps(groups))  # serialisable
