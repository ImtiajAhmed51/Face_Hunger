"""Event detection: synthetic trips, incremental re-runs, user overrides and undo."""

from datetime import datetime, timedelta

import numpy as np
import pytest

from backend.services import events as ev

HOME = (23.8103, 90.4125)       # Dhaka
COX = (21.4272, 92.0058)        # Cox's Bazar, ~290 km away
PARIS = (48.8566, 2.3522)
SYLHET = (24.8949, 91.8687)     # ~200 km away


def add(db, rows):
    """rows: (captured_at, lat, lon). Returns ids."""
    with db.connect() as conn:
        conn.execute("INSERT OR IGNORE INTO libraries(id, path, name) VALUES (1, '/lib', 'lib')")
        start = conn.execute("SELECT COALESCE(MAX(id), 0) FROM media").fetchone()[0] + 1
        ids = []
        for offset, (when, lat, lon) in enumerate(rows):
            mid = start + offset
            conn.execute("INSERT INTO media(id, library_id, path, name, kind, size, mtime_ns, captured_at, gps_lat, gps_lon, status)"
                         " VALUES (?, 1, ?, ?, 'photo', 1, 1, ?, ?, ?, 'indexed')",
                         (mid, f"/lib/{mid}.jpg", f"{mid}.jpg", when.isoformat(), lat, lon))
            ids.append(mid)
    return ids


def day(date: str, place, hours=range(9, 21, 2), jitter=0.01, no_gps_every=0):
    base = datetime.fromisoformat(date)
    out = []
    for n, h in enumerate(hours):
        lat, lon = place
        gps = not (no_gps_every and n % no_gps_every == 0)
        out.append((base + timedelta(hours=h, minutes=7 * n), lat + jitter * np.sin(n) if gps else None,
                    lon + jitter * np.cos(n) if gps else None))
    return out


def trip_fixture(db):
    """3 trips + 2 home days (and everyday home photos) -> exactly 5 events."""
    rows = []
    rows += day("2024-01-05", HOME)                                            # home day 1
    rows += day("2024-02-10", COX) + day("2024-02-11", COX, no_gps_every=3) + day("2024-02-12", COX)   # trip 1
    rows += day("2024-03-01", HOME, hours=range(10, 16))                      # home day 2
    rows += sum((day(f"2024-05-0{d}", PARIS, jitter=0.03) for d in range(1, 6)), [])                  # trip 2
    rows += day("2024-07-10", SYLHET) + day("2024-07-11", SYLHET)             # trip 3
    return add(db, rows)


def events(db):
    return db.all("SELECT * FROM events ORDER BY start_at")


def test_trip_fixture_yields_exactly_five_events(app_services):
    s = app_services
    trip_fixture(s.db)
    result = s.events.detect()
    found = events(s.db)
    assert len(found) == 5, [(e["name"], e["item_count"]) for e in found]
    assert [e["item_count"] for e in found] == [6, 18, 6, 30, 12]
    assert found[1]["start_at"].startswith("2024-02-10") and found[1]["end_at"].startswith("2024-02-12")
    assert all(e["cover_media_id"] for e in found)
    # Without a place dataset, names fall back to coordinates + dates.
    assert "°N" in found[3]["name"] and "May 2024" in found[3]["name"]
    assert result["events_touched"] == 5
    # Every media belongs to exactly one event.
    assert s.db.one("SELECT COUNT(*) c FROM event_media")["c"] == 72


def test_place_names_from_offline_dataset(app_services, tmp_path):
    geo = app_services.config.model_dir / "geonames"
    geo.mkdir(parents=True)
    (geo / "cities15000.txt").write_text(
        "1\tParis\tParis\t\t48.85341\t2.3488\tP\tPPLC\tFR\n2\tCox's Bazar\tCox's Bazar\t\t21.45324\t91.97504\tP\tPPLA2\tBD\n",
        encoding="utf-8")
    app_services.events = ev.EventService(app_services)
    trip_fixture(app_services.db)
    app_services.events.detect()
    names = [e["name"] for e in events(app_services.db)]
    assert names[1].startswith("Cox's Bazar · 10–12 Feb 2024")
    assert names[3].startswith("Paris · 1–5 May 2024")


def test_incremental_rerun_only_touches_affected_events(app_services):
    s = app_services
    ids = trip_fixture(s.db)
    s.events.detect()
    before = {e["id"]: e["updated_at"] for e in events(s.db)}
    paris = next(e for e in events(s.db) if e["start_at"].startswith("2024-05-01"))
    with s.db.connect() as conn:
        conn.execute("UPDATE events SET updated_at='2000-01-01 00:00:00'")
    # Two more photos on the last Paris day.
    add(s.db, day("2024-05-05", PARIS, hours=[21, 22]))
    result = s.events.detect(since_media_id=max(ids))
    after = {e["id"]: e for e in events(s.db)}
    assert result["events_touched"] == 1
    assert after[paris["id"]]["item_count"] == 32  # same event id, grown
    assert [eid for eid, e in after.items() if e["updated_at"] != "2000-01-01 00:00:00"] == [paris["id"]]
    assert set(after) == set(before)
    # A brand-new home day becomes a new event and touches nothing else.
    top = s.db.one("SELECT MAX(id) m FROM media")["m"]
    add(s.db, day("2024-09-09", HOME, hours=range(12, 15)))
    result = s.events.detect(since_media_id=top)
    assert result["events_touched"] == 1 and len(events(s.db)) == 6


def test_user_overrides_survive_redetection_and_undo_restores(client, app_services):
    s = app_services
    trip_fixture(s.db)
    s.events.detect()
    home1, cox, home2, paris, sylhet = events(s.db)

    renamed = client.patch(f"/api/events/{cox['id']}", json={"name": "Beach week"}).json()
    merged = client.post("/api/events/merge", json={"event_ids": [sylhet["id"], home2["id"]]}).json()
    paris_items = [r["media_id"] for r in s.db.all(
        "SELECT em.media_id FROM event_media em JOIN media m ON m.id=em.media_id WHERE em.event_id=? "
        "ORDER BY m.captured_at", (paris["id"],))]
    split = client.post(f"/api/events/{paris['id']}/split", json={"media_id": paris_items[18]}).json()
    moved = client.post(f"/api/events/{home1['id']}/move", json={"media_ids": paris_items[:2]}).json()
    assert split["new_event"]["item_count"] == 12 and moved["item_count"] == 8

    snapshot = {e["id"]: (e["name"], e["item_count"]) for e in events(s.db)}
    s.events.detect()  # full re-detection: user edits win
    assert {e["id"]: (e["name"], e["item_count"]) for e in events(s.db)} == snapshot
    assert s.db.one("SELECT name FROM events WHERE id=?", (cox["id"],))["name"] == "Beach week"

    # Undo in reverse order restores the detected state exactly.
    for token in (moved["undo_token"], split["undo_token"], merged["undo_token"], renamed["undo_token"]):
        assert client.post(f"/api/events/undo/{token}").json()["ok"]
    restored = events(s.db)
    assert [(e["id"], e["item_count"]) for e in restored] == [(e["id"], e["item_count"]) for e in (home1, cox, home2, paris, sylhet)]
    assert restored[1]["name"] == cox["name"]
    assert client.post(f"/api/events/undo/{renamed['undo_token']}").status_code == 404  # already undone
    listed = client.get("/api/events").json()
    assert listed["total"] == 5 and listed["items"][0]["id"] == sylhet["id"]  # newest first
    assert client.get(f"/api/media?event={paris['id']}&limit=200").json()["total"] == 30


def test_visual_coherence_splits_and_people_overlap_merges():
    t0 = datetime(2024, 4, 1, 9)
    a, b = np.eye(8, dtype=np.float32)[0], np.eye(8, dtype=np.float32)[1]
    same_place = [ev.Item(i, t0 + timedelta(minutes=20 * i), 23.8, 90.4, a if i < 5 else b) for i in range(10)]
    # 20-min spacing never reaches the 90-min visual gap: one event.
    assert len(ev.cluster(same_place)) == 1
    gapped = [ev.Item(i, t0 + timedelta(minutes=20 * i + (100 if i >= 5 else 0)), 23.8, 90.4, a if i < 5 else b)
              for i in range(10)]
    assert [len(g) for g in ev.cluster(gapped)] == [5, 5]  # 100-min gap + different scene: split
    alike = [ev.Item(i, t0 + timedelta(minutes=20 * i + (100 if i >= 5 else 0)), 23.8, 90.4, a) for i in range(10)]
    assert len(ev.cluster(alike)) == 1  # same scene: kept together
    # 5 h apart (beyond the 4 h link) but the same people, same day, same place: merged.
    party = [ev.Item(1, t0, None, None, None, frozenset({7, 8})), ev.Item(2, t0 + timedelta(hours=5), None, None, None, frozenset({7, 8}))]
    assert len(ev.cluster(party)) == 1
    strangers = [ev.Item(1, t0, None, None, None, frozenset({7})), ev.Item(2, t0 + timedelta(hours=5), None, None, None, frozenset({9}))]
    assert len(ev.cluster(strangers)) == 2


@pytest.mark.parametrize("start,end,label", [
    ("2024-02-10T09:00", "2024-02-10T20:00", "10 Feb 2024"),
    ("2024-02-10T09:00", "2024-02-12T20:00", "10–12 Feb 2024"),
    ("2024-01-30T09:00", "2024-02-02T20:00", "30 Jan – 2 Feb 2024"),
    ("2023-12-30T09:00", "2024-01-02T20:00", "30 Dec 2023 – 2 Jan 2024"),
])
def test_date_range_labels(start, end, label):
    assert ev.date_range_label(datetime.fromisoformat(start), datetime.fromisoformat(end)) == label


def test_detection_job_is_incremental(app_services):
    s = app_services
    trip_fixture(s.db)
    s.jobs.start()
    first = s.jobs.enqueue("event_detection")
    assert s.jobs.wait(first["id"])["status"] == "completed"
    assert len(events(s.db)) == 5
    s.schedule_events()  # nothing new: no job
    assert s.db.one("SELECT COUNT(*) c FROM jobs WHERE kind='event_detection'")["c"] == 1
    add(s.db, day("2024-12-24", HOME, hours=range(18, 22)))
    s.schedule_events()
    job = s.db.one("SELECT * FROM jobs WHERE kind='event_detection' ORDER BY id DESC LIMIT 1")
    done = s.jobs.wait(job["id"])
    assert __import__("json").loads(done["progress"])["full"] is False and len(events(s.db)) == 6


def test_file_time_dates_do_not_form_events(app_services):
    s = app_services
    trip_fixture(s.db)
    bulk = add(s.db, day("2025-06-01", HOME, hours=range(8, 20)))  # a bulk copy: only file times known
    with s.db.connect() as conn:
        conn.execute(f"UPDATE media SET date_source='mtime' WHERE id IN ({','.join(map(str, bulk))})")
    s.events.detect()
    assert len(events(s.db)) == 5
    assert not s.db.one(f"SELECT 1 x FROM event_media WHERE media_id IN ({','.join(map(str, bulk))})")
