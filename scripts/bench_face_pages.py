"""Disposable person-face paging benchmark. Run: PYTHONPATH=. python scripts/bench_face_pages.py.

Compares the legacy 24-source waterfall (240 faces) with a bounded 60-face page.
Uses TestClient timings rather than browser latency; no inference or live data.
"""
import os

os.environ["NO_ALBUMENTATIONS_UPDATE"] = "1"
import statistics
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter

from fastapi.testclient import TestClient

from backend.app import create_app
from backend.config import Config
from backend.services.container import Services
from tests.conftest import FakeEngine
from tests.helpers.fixtures import add_media

with TemporaryDirectory(prefix="fh-bench-") as root:
    cfg = Config(
        data_dir=Path(root) / "data", model_dir=Path(root) / "models", allowed_roots=root, allowed_hosts="testserver", watch=False
    )
    services = Services(cfg, engine=FakeEngine())
    ids = add_media(services.db, 24)
    with services.db.connect() as c:
        c.execute("INSERT INTO people(id,name,face_count) VALUES (1,'Benchmark',240)")
        c.executemany(
            "INSERT INTO faces(media_id,person_id,bbox,detection,embedding_offset,embedding_sha) VALUES (?,1,'[0,0,1,1]',0.9,0,'none')",
            [(mid,) for mid in ids for _ in range(10)],
        )
    app = create_app(services=services)
    client = TestClient(app)

    def old():
        page = client.get("/api/media?people=1&excluded=true&limit=24").json()
        responses = [client.get(f"/api/media/{m['id']}") for m in page["items"]]
        return 25, sum(len(r.content) for r in responses)

    def new():
        r = client.get("/api/people/1/faces?limit=60")
        return 1, len(r.content)

    for name, fn in [("legacy", old), ("bounded", new)]:
        fn()
        times = []
        for _ in range(5):
            start = perf_counter()
            requests, payload = fn()
            times.append((perf_counter() - start) * 1000)
        print(f"{name}: median={statistics.median(times):.1f}ms requests={requests} payload={payload}B")
    services.close()
