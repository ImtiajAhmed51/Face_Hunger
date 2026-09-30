"""Capture metadata extraction, resumable backfill, timeline buckets, map points and PMTiles."""

import gzip
import json
import struct
from fractions import Fraction

import pytest
from PIL import Image

from backend import metadata as meta
from tests.helpers.fixtures import add_media


def exif_jpeg(path, *, date=None, gps=None, make=None, model=None, lens=None):
    exif = Image.Exif()
    if date:
        exif[0x0132] = date
        exif.get_ifd(0x8769)[36867] = date
    if make:
        exif[0x010F] = make
    if model:
        exif[0x0110] = model
    if lens:
        exif.get_ifd(0x8769)[42036] = lens
    if gps:
        lat, lon = gps

        def dms(value):
            value = abs(value)
            d = int(value)
            m = int((value - d) * 60)
            s = Fraction(round(((value - d) * 60 - m) * 60 * 1000), 1000)
            return (Fraction(d), Fraction(m), s)

        exif.get_ifd(0x8825).update({1: "N" if lat >= 0 else "S", 2: dms(lat), 3: "E" if lon >= 0 else "W", 4: dms(lon)})
    Image.new("RGB", (32, 24), (90, 90, 90)).save(path, exif=exif.tobytes())


def test_exif_date_gps_camera(tmp_path):
    path = tmp_path / "a.jpg"
    exif_jpeg(path, date="2021:06:01 14:30:05", gps=(-33.8688, 151.2093), make="FUJIFILM", model="X-T3",
              lens="XF23mmF2 R WR")
    out = meta.extract(path, "photo")
    assert out["captured_at"] == "2021-06-01T14:30:05" and out["date_source"] == "exif"
    assert out["gps_lat"] == pytest.approx(-33.8688, abs=1e-4) and out["gps_lon"] == pytest.approx(151.2093, abs=1e-4)
    assert (out["camera_make"], out["camera_model"], out["lens"]) == ("FUJIFILM", "X-T3", "XF23mmF2 R WR")


@pytest.mark.parametrize("name,expected", [
    ("IMG_20190918_164153.jpg", "2019-09-18T16:41:53"),
    ("PXL_20230425_125050434.jpg", "2023-04-25T12:50:50"),
    ("2021-06-01 12.30.00.jpg", "2021-06-01T12:30:00"),
    ("IMG-20200101-WA0001.jpg", "2020-01-01T12:00:00"),
    ("Screenshot_2020-02-03-10-11-12.png", "2020-02-03T10:11:12"),
    ("VID_20221231_235959.mp4", "2022-12-31T23:59:59"),
    ("holiday.jpg", None),
    ("IMG_1234.jpg", None),
    ("20991231_000000.jpg", None),  # future: implausible
])
def test_filename_dates(name, expected):
    assert meta.date_from_filename(name) == expected


def test_fallback_chain_filename_then_mtime(tmp_path):
    named = tmp_path / "IMG_20190918_164153.jpg"
    Image.new("RGB", (8, 8)).save(named)
    assert meta.extract(named, "photo")["date_source"] == "filename"
    plain = tmp_path / "plain.jpg"
    Image.new("RGB", (8, 8)).save(plain)
    out = meta.extract(plain, "photo", mtime_ns=1_600_000_000 * 10**9)
    assert out["date_source"] == "mtime" and out["captured_at"].startswith("2020-09-1")
    bad = tmp_path / "bad.jpg"
    exif_jpeg(bad, date="0000:00:00 00:00:00")
    assert meta.extract(bad, "photo")["date_source"] == "mtime"


def test_iso6709_and_gps_edge_cases():
    assert meta.parse_iso6709("+37.7749-122.4194+010.000/") == (37.7749, -122.4194, 10.0)
    assert meta.parse_iso6709("+00.0000+000.0000/") == (None, None, None)  # null island
    assert meta.gps_from_exif({}) == (None, None, None)


def test_metadata_backfill_job_is_resumable(app_services, tmp_path):
    s = app_services
    lib = tmp_path / "lib"
    lib.mkdir()
    ids = add_media(s.db, 1200, library_path=str(lib))
    for i in ids[:5]:
        exif_jpeg(lib / f"img_{i}.jpg", date="2018:07:0%d 10:00:00" % (1 + i % 9), gps=(51.5, -0.12))
    with s.db.connect() as conn:  # rows predate migration 9
        conn.execute("UPDATE media SET meta_version=0, captured_at=NULL")
    s.jobs.start()
    job = s.jobs.enqueue("metadata_backfill")
    if s.jobs.wait(job["id"], statuses=("running", "completed"))["status"] == "running":
        s.jobs.control(job["id"], "cancel")
    s.jobs.wait(job["id"])
    remaining = s.db.one("SELECT COUNT(*) c FROM media WHERE meta_version=0")["c"]
    again = s.jobs.enqueue("metadata_backfill")
    done = s.jobs.wait(again["id"], timeout=60)
    assert done["status"] == "completed"
    # Resumable: the second run only touched what the first did not finish.
    assert json.loads(done["progress"])["updated"] == remaining
    rows = s.db.all("SELECT id, captured_at, date_source, gps_lat FROM media ORDER BY id")
    assert all(r["captured_at"] for r in rows)
    assert [r["date_source"] for r in rows[:5]] == ["exif"] * 5 and rows[0]["gps_lat"] == pytest.approx(51.5)
    assert {r["date_source"] for r in rows[5:]} == {"mtime"}  # files absent: stored mtime used
    # Re-running with nothing pending is a no-op.
    third = s.jobs.enqueue("metadata_backfill")
    assert json.loads(s.jobs.wait(third["id"])["progress"])["updated"] == 0


def test_timeline_matches_media_order(client, app_services):
    add_media(app_services.db, 90, kind="mixed")
    tl = client.get("/api/timeline").json()
    assert tl["total"] == 90 and sum(n for _, n in tl["days"]) == 90
    assert [d for d, _ in tl["days"]] == sorted((d for d, _ in tl["days"]), reverse=True)
    # Walking /api/media?sort=date yields items day by day in exactly the bucket sizes.
    items = client.get("/api/media?sort=date&limit=200").json()["items"]
    offset = 0
    for day, count in tl["days"]:
        assert {i["captured_at"][:10] for i in items[offset:offset + count]} == {day}
        offset += count
    assert client.get("/api/timeline?kind=video").json()["total"] == 18


def test_map_points_and_bbox_filter(client, app_services):
    ids = add_media(app_services.db, 10)
    with app_services.db.connect() as conn:
        for i, (lat, lon) in zip(ids, [(48.85, 2.35), (48.86, 2.34), (40.71, -74.0), (35.68, 139.69)]):
            conn.execute("UPDATE media SET gps_lat=?, gps_lon=? WHERE id=?", (lat, lon, i))
    pts = client.get("/api/map/points").json()
    assert pts["count"] == 4 and len(pts["ids"]) == len(pts["lat"]) == len(pts["lon"]) == 4
    paris = client.get("/api/media?bbox=2.0,48.0,3.0,49.0").json()
    assert {i["id"] for i in paris["items"]} == set(ids[:2])
    assert client.get("/api/media?bbox=170,-10,-170,10").json()["total"] == 0  # antimeridian form accepted
    assert client.get("/api/media?bbox=nope").status_code == 400


def _pmtiles(path, *, tile_type=1, layers=("earth", "water")):
    meta_json = gzip.compress(json.dumps({"vector_layers": [{"id": n} for n in layers], "name": "t"}).encode())
    header = bytearray(127)
    header[:7] = b"PMTiles"
    header[7] = 3
    struct.pack_into("<QQ", header, 24, 127, len(meta_json))
    header[97], header[99], header[100], header[101] = 2, tile_type, 0, 14
    struct.pack_into("<iiii", header, 102, int(-10e7), int(40e7), int(5e7), int(55e7))
    path.write_bytes(bytes(header) + meta_json)


def test_map_tiles_are_off_by_default_and_local_only(client, tmp_path):
    cfg = client.get("/api/map/config").json()
    assert cfg == {"tiles_enabled": False, "pmtiles": None, "pmtiles_url": None, "error": None}
    assert client.get("/api/map/tiles.pmtiles").status_code == 404
    tiles = tmp_path / "europe.pmtiles"
    _pmtiles(tiles)
    assert client.patch("/api/settings", json={"map_pmtiles_path": str(tmp_path / "x.mbtiles")}).status_code == 400
    assert client.patch("/api/settings", json={"map_tiles_enabled": True, "map_pmtiles_path": str(tiles)}).status_code == 200
    cfg = client.get("/api/map/config").json()
    assert cfg["pmtiles"]["tile_type"] == "mvt" and cfg["pmtiles"]["vector_layers"] == ["earth", "water"]
    assert cfg["pmtiles"]["bounds"] == [-10.0, 40.0, 5.0, 55.0] and cfg["pmtiles_url"] == "/api/map/tiles.pmtiles"
    ranged = client.get("/api/map/tiles.pmtiles", headers={"Range": "bytes=0-6"})
    assert ranged.status_code == 206 and ranged.content == b"PMTiles"
