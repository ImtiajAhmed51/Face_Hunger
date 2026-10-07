"""Run inside an old checkout: build a small data_dir with that version's own code."""
import json
import sys
from pathlib import Path

import numpy as np

out = Path(sys.argv[1]); out.mkdir(parents=True)
from backend.config import Config
from backend.services.container import Services
from tests.conftest import FakeEngine

(out / "fe").mkdir()
cfg = Config(data_dir=out / "data", model_dir=out / "models", frontend_dir=out / "fe", allowed_roots=str(out), watch=False)
s = Services(cfg, engine=FakeEngine())
rng = np.random.default_rng(42)
def unit(d):
    v = rng.standard_normal(d).astype(np.float32); return v / np.linalg.norm(v)
with s.db.connect() as conn:
    conn.execute("INSERT INTO libraries(id, path, name) VALUES (1, '/photos/family', 'Family')")
    conn.execute("INSERT INTO people(id, name, face_count) VALUES (1, 'Ada', 6), (2, 'Grace', 4), (3, NULL, 2)")
    for i in range(1, 41):
        conn.execute("INSERT INTO media(id, library_id, path, name, kind, size, mtime_ns, status) VALUES (?,1,?,?,?,?,?,'indexed')",
                     (i, f"/photos/family/img_{i}.jpg", f"img_{i}.jpg", "video" if i % 10 == 0 else "photo", 1000 + i, i))
faces = []
for i in range(1, 13):
    offset, sha = s.store.append(unit(512))
    faces.append((i, 1 if i <= 6 else 2 if i <= 10 else 3, offset, sha))
with s.db.connect() as conn:
    conn.executemany("INSERT INTO faces(media_id, person_id, bbox, detection, embedding_offset, embedding_sha) VALUES (?,?,'[1,2,30,40]',0.93,?,?)", faces)
    conn.execute("INSERT INTO settings(key, value) VALUES ('matching_threshold', '0.47') ON CONFLICT(key) DO UPDATE SET value=excluded.value")
    cols = {r[1] for r in conn.execute("PRAGMA table_info(saved_searches)")}
    if cols:
        conn.execute("INSERT INTO saved_searches(name, query) VALUES ('Ada in photos', ?)", (json.dumps({"people": [1], "kind": "photo"}),))
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "albums" in tables:
        conn.execute("INSERT INTO albums(id, name) VALUES (1, 'Summer')")
        conn.executemany("INSERT INTO album_media(album_id, media_id, position) VALUES (1, ?, ?)", [(i, i) for i in range(1, 6)])
        conn.execute("INSERT INTO favorites(media_id) VALUES (3)")
    version = conn.execute("PRAGMA user_version").fetchone()[0]
try:
    from backend.vectors.specs import ModelSpec
    space = s.vectors.register(ModelSpec("fixture-visual", "1", 16, "media", "visual"))
    space.add([(i, unit(16)) for i in range(1, 41)])
    space.maybe_save(force=True) if hasattr(space, "maybe_save") else None
except Exception as exc:
    print("vectors:", exc)
s.close()
print("schema", version, sorted(p.name for p in (out / "data").iterdir()))
