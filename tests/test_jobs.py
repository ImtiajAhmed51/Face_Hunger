"""Job system: priorities, pause/resume/cancel, crash recovery, SSE and the file watcher."""

import json
import shutil
import time

import pytest
from PIL import Image

from backend.jobs.manager import PRIORITY, JobManager
from tests.helpers.fixtures import FAKE_VISUAL, FakeEmbedder, add_media


@pytest.fixture
def jobs(app_services):
    app_services.jobs.start()
    return app_services


def backfill_setup(s, n=200, delay=0.05):
    add_media(s.db, n)
    s.extra_embedders[FAKE_VISUAL.key] = FakeEmbedder(FAKE_VISUAL, delay=delay)
    return s.vectors.register(FAKE_VISUAL)


def test_backfill_job_runs_to_completion_with_progress(jobs):
    space = backfill_setup(jobs, 60, delay=0)
    job = jobs.jobs.enqueue("embed_backfill", priority=PRIORITY["background"])
    done = jobs.jobs.wait(job["id"])
    assert done["status"] == "completed" and space.count() == 60
    progress = json.loads(done["progress"])
    assert progress["embedded"] == 60 and progress["processed"] == progress["total"] == 60


def test_urgent_job_preempts_background_job_which_then_resumes(jobs):
    space = backfill_setup(jobs, 300, delay=0.03)
    order = []
    jobs.jobs.register("probe", lambda ctx: order.append(("probe", space.count())) or {})
    low = jobs.jobs.enqueue("embed_backfill", priority=PRIORITY["background"])
    jobs.jobs.wait(low["id"], statuses=("running",))
    time.sleep(0.2)
    high = jobs.jobs.enqueue("probe", priority=PRIORITY["urgent"])
    jobs.jobs.wait(high["id"])
    done = jobs.jobs.wait(low["id"], timeout=60)
    assert done["status"] == "completed" and space.count() == 300
    assert 0 < order[0][1] < 300  # probe ran mid-backfill
    assert done["attempts"] == 0


def test_cancel_stops_within_two_seconds(jobs):
    backfill_setup(jobs, 500, delay=0.3)
    job = jobs.jobs.enqueue("embed_backfill")
    jobs.jobs.wait(job["id"], statuses=("running",))
    time.sleep(0.5)
    started = time.monotonic()
    jobs.jobs.control(job["id"], "cancel")
    done = jobs.jobs.wait(job["id"], timeout=5)
    assert done["status"] == "cancelled"
    assert time.monotonic() - started < 2.0


def test_pause_and_resume(jobs):
    space = backfill_setup(jobs, 120, delay=0.02)
    job = jobs.jobs.enqueue("embed_backfill")
    jobs.jobs.wait(job["id"], statuses=("running",))
    jobs.jobs.control(job["id"], "pause")
    time.sleep(0.3)
    frozen = space.count()
    time.sleep(0.4)
    assert space.count() == frozen < 120
    assert jobs.jobs.get(job["id"])["status"] == "paused"
    jobs.jobs.control(job["id"], "resume")
    assert jobs.jobs.wait(job["id"], timeout=30)["status"] == "completed" and space.count() == 120


def test_queued_job_pause_and_cancel_without_running(app_services):
    m = app_services.jobs  # runner not started
    job = m.enqueue("embed_backfill")
    assert m.control(job["id"], "pause")["status"] == "paused"
    assert m.control(job["id"], "resume")["status"] == "queued"
    assert m.control(job["id"], "cancel")["status"] == "cancelled"


def test_job_state_survives_restart(app_services):
    db = app_services.db
    m = app_services.jobs
    running = m.enqueue("embed_backfill")
    paused = m.enqueue("rebuild_index", {"key": "x"})
    queued = m.enqueue("ingest", {"paths": ["/a.jpg"]}, priority=10)
    m.control(paused["id"], "pause")
    with db.connect() as conn:  # simulate a process killed while a job was running
        conn.execute("UPDATE jobs SET status='running', progress=? WHERE id=?",
                     (json.dumps({"processed": 7, "total": 9}), running["id"]))
    fresh = JobManager(db)
    assert fresh.recovered == [running["id"]]
    after = {j["id"]: j for j in db.all("SELECT * FROM jobs")}
    assert after[running["id"]]["status"] == "queued" and after[running["id"]]["attempts"] == 1
    assert json.loads(after[running["id"]]["progress"]) == {"processed": 7, "total": 9}
    assert after[paused["id"]]["status"] == "paused"
    assert after[queued["id"]]["status"] == "queued" and json.loads(after[queued["id"]]["payload"]) == {"paths": ["/a.jpg"]}
    for _ in range(2):  # a job that keeps crashing is eventually failed, not retried forever
        with db.connect() as conn:
            conn.execute("UPDATE jobs SET status='running' WHERE id=?", (running["id"],))
        JobManager(db)
    assert db.one("SELECT status FROM jobs WHERE id=?", (running["id"],))["status"] == "failed"


def test_ingest_jobs_are_deduplicated_and_merged(app_services):
    m = app_services.jobs
    from backend.jobs.handlers import merge_paths
    a = m.enqueue("ingest", {"paths": ["/x/1.jpg"]}, dedupe_key="ingest", merge=merge_paths)
    b = m.enqueue("ingest", {"paths": ["/x/2.jpg", "/x/1.jpg"]}, dedupe_key="ingest", merge=merge_paths)
    assert a["id"] == b["id"] and json.loads(b["payload"])["paths"] == ["/x/1.jpg", "/x/2.jpg"]


def test_jobs_api_and_sse_stream(client, app_services):
    backfill_setup(app_services, 30, delay=0)
    r = client.post("/api/jobs", json={"kind": "embed_backfill", "priority": 80})
    assert r.status_code == 200 and r.json()["kind"] == "embed_backfill"
    assert client.post("/api/jobs", json={"kind": "rm -rf"}).status_code == 400
    with client.stream("GET", "/api/jobs/events?max_seconds=1.5") as stream:
        body = "".join(stream.iter_text())
    events = [json.loads(line[6:]) for line in body.splitlines() if line.startswith("data: ")]
    assert any(e["id"] == r.json()["id"] for e in events)
    listed = client.get("/api/jobs").json()
    assert listed["watcher"]["running"] is True
    job_id = r.json()["id"]
    assert client.get(f"/api/jobs/{job_id}").json()["id"] == job_id
    assert client.post(f"/api/jobs/{job_id}/explode").status_code == 404


def _jpeg(path, color=(200, 30, 30)):
    Image.new("RGB", (64, 48), color).save(path, "JPEG")


def test_watcher_indexes_a_copied_in_photo_within_5s(client, app_services, tmp_path):
    library = tmp_path / "library"
    library.mkdir()
    assert client.post("/api/libraries", json={"path": str(library)}).status_code == 200
    source = tmp_path / "outside.jpg"
    _jpeg(source)
    time.sleep(0.3)  # let the observer attach
    started = time.monotonic()
    shutil.copy(source, library / "holiday.jpg")
    row = None
    while time.monotonic() - started < 5:
        row = app_services.db.one("SELECT * FROM media WHERE name='holiday.jpg' AND status='indexed'")
        if row:
            break
        time.sleep(0.05)
    elapsed = time.monotonic() - started
    assert row is not None, f"not indexed after {elapsed:.1f}s"
    print(f"watcher latency {elapsed:.2f}s")
    assert row["width"] == 64 and row["content_hash"]
    assert (app_services.config.data_dir / "thumbnails" / f"media-{row['id']}.jpg").is_file()

    # Deleting the file marks it missing (never deletes the row or anything on disk).
    (library / "holiday.jpg").unlink()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if app_services.db.one("SELECT missing FROM media WHERE id=?", (row["id"],))["missing"]:
            break
        time.sleep(0.05)
    assert app_services.db.one("SELECT missing FROM media WHERE id=?", (row["id"],))["missing"] == 1
