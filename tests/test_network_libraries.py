"""Libraries on network shares: path identity, junk filters, offline vs missing, incremental scans,
relink, fault injection, share connection errors and the read-only guarantee.

No SMB server is available in this environment, so the "share" is a local folder registered as a
network library, with latency, timeouts and disconnects injected into the I/O layer the scanner uses.
"""

import errno
import hashlib
import json
import os
import shutil
import threading
import time
import unicodedata
from contextlib import contextmanager
from pathlib import Path

import pytest
from PIL import Image

from backend import netlib, netscan
from backend.netlib import ReadOnlyLibrary
from backend.services.shares import ShareError, ShareService

MOUNT_DARWIN = """/dev/disk3s1s1 on / (apfs, sealed, local, read-only, journaled)
/dev/disk3s5 on /System/Volumes/Data (apfs, local, journaled, nobrowse)
//imtiaj@192.168.110.81/E on /Volumes/E (smbfs, nodev, nosuid, mounted by me)
//GUEST:@Windows-PC._smb._tcp.local/Family%20Photos on /Volumes/Family Photos (smbfs, nodev, nosuid, read-only, mounted by me)
nas.local:/volume1/photo on /Volumes/photo (nfs, nodev, nosuid, mounted by me)
/dev/disk5s1 on /Volumes/USB DRIVE (exfat, local, nodev, nosuid, noowners)
"""
MOUNT_LINUX = """/dev/nvme0n1p2 / ext4 rw,relatime 0 0
//192.168.1.20/photos /mnt/photos cifs ro,relatime,vers=3.1.1,username=me 0 0
nas:/export/media /media/nas nfs4 rw,relatime 0 0
/dev/sdb1 /run/media/me/My\\040Disk vfat rw 0 0
"""


# -- unit: path identity -------------------------------------------------------------------------
def test_relative_identity_is_the_same_across_spellings_and_systems():
    nfc, nfd = "Café/Été 2024/IMG_0001.JPG", unicodedata.normalize("NFD", "Café/Été 2024/IMG_0001.JPG")
    assert nfc != nfd
    mac = netlib.relative_identity("/Volumes/photos", f"/Volumes/photos/{nfd}")
    remount = netlib.relative_identity("/Volumes/photos-1", f"/Volumes/photos-1/{nfc}")
    windows = netlib.relative_identity("\\\\PC\\Photos", "\\\\pc\\photos\\café\\été 2024\\img_0001.jpg")
    long_unc = netlib.relative_identity("\\\\?\\UNC\\PC\\Photos", "\\\\?\\UNC\\PC\\Photos\\Café\\Été 2024\\IMG_0001.JPG")
    drive = netlib.relative_identity("Z:\\", "Z:\\Café\\Été 2024\\IMG_0001.JPG")
    linux = netlib.relative_identity("/mnt/photos/", "/mnt/photos//Café/Été 2024/IMG_0001.JPG")
    assert mac[0] == remount[0] == "Café/Été 2024/IMG_0001.JPG"  # stored NFC, forward slashes
    assert len({mac[1], remount[1], windows[1], long_unc[1], drive[1], linux[1]}) == 1
    # Windows ignores trailing dots and spaces in names.
    assert netlib.relative_identity("Z:\\", "Z:\\Trip. \\a.jpg")[1] == netlib.relative_identity("/m", "/m/Trip/a.jpg")[1]
    with pytest.raises(ValueError):
        netlib.relative_identity("/Volumes/photos", "/Volumes/other/a.jpg")
    assert netlib.join_root("/Volumes/photos-1", "Café/a.jpg") == "/Volumes/photos-1/Café/a.jpg"
    assert netlib.join_root("\\\\PC\\Photos", "Café/a.jpg") == "\\\\PC\\Photos\\Café\\a.jpg"
    assert netlib.join_root("Z:\\", "a/b.jpg") == "Z:\\a\\b.jpg"


def test_junk_filters():
    for name in ("$RECYCLE.BIN", "System Volume Information", ".Trashes", ".Spotlight-V100", "@eaDir", "#recycle", "lost+found", ".hidden", "$recycle.bin"):
        assert netlib.is_junk_dir(name), name
    for name in ("._IMG_1.jpg", ".DS_Store", "Thumbs.db", "thumbs.db", "desktop.ini", "movie.mp4.part", "x.crdownload"):
        assert netlib.is_junk_file(name), name
    for name in ("Holiday", "2024", "Recycle photos"):
        assert not netlib.is_junk_dir(name)
    for name in ("IMG_1.jpg", "desktop.jpg", "_IMG.jpg"):
        assert not netlib.is_junk_file(name)


def test_mount_tables_and_classification():
    mounts = netlib.parse_mounts(MOUNT_DARWIN, "darwin")
    shares = {m["mount_point"]: m for m in mounts if m["network"]}
    assert shares["/Volumes/E"] | {} == shares["/Volumes/E"] and shares["/Volumes/E"]["server"] == "192.168.110.81" and shares["/Volumes/E"]["share"] == "E"
    assert "imtiaj" not in json.dumps(mounts) and "GUEST" not in json.dumps(mounts)  # user names are not kept
    assert shares["/Volumes/Family Photos"]["share"] == "Family Photos" and shares["/Volumes/Family Photos"]["read_only"] is True
    assert shares["/Volumes/photo"]["protocol"] == "nfs" and shares["/Volumes/photo"]["server"] == "nas.local"
    assert netlib.classify("/Volumes/E/2024/trip", mounts) == {"kind": "network", "protocol": "smb", "server": "192.168.110.81", "share": "E", "mount_point": "/Volumes/E"}
    assert netlib.classify("/Volumes/USB DRIVE/DCIM", mounts)["kind"] == "external"
    assert netlib.classify("/Users/me/Pictures", mounts)["kind"] == "local"
    linux = netlib.parse_mounts(MOUNT_LINUX, "linux")
    assert netlib.classify("/mnt/photos/a", linux)["kind"] == "network" and netlib.classify("/mnt/photos/a", linux)["share"] == "photos"
    assert netlib.classify("/media/nas", linux)["protocol"] == "nfs"
    assert netlib.classify("/run/media/me/My Disk/x", linux)["kind"] == "external"
    assert netlib.classify("\\\\PC\\Share\\x")["kind"] == "network"


@pytest.mark.parametrize("output,code", [
    ("mount_smbfs: server rejected the connection: Authentication error", "auth"),
    ("execution error: An error of type -5045 has occurred. (-5045)", "auth"),
    ("System error 1326 has occurred. The user name or password is incorrect.", "auth"),
    ("mount_smbfs: server connection failed: No route to host", "unreachable"),
    ("mount_smbfs: could not connect to 10.0.0.9: Host is down", "unreachable"),
    ("System error 53 has occurred. The network path was not found.", "unreachable"),
    ("mount_smbfs: mount error: /Volumes/x: No such file or directory", "share_not_found"),
    ("System error 67 has occurred. The network name cannot be found.", "share_not_found"),
    ("mount error(13): Permission denied", "permission"),
    ("mount_smbfs: server connection failed: Connection refused", "firewall"),
    ("mount error(95): Operation not supported", "protocol"),
    ("mount_smbfs: server connection failed: Operation timed out", "timeout"),
    ("mount_smbfs: mount error: /Volumes/x: File exists", "already"),
    ("something nobody has seen before", "unknown"),
])
def test_mount_errors_map_to_actionable_messages(output, code):
    error = netlib.map_mount_error(output)
    assert error["code"] == code and error["message"] and error["next_step"]
    assert "error" not in error["message"].lower() or code == "unknown"


def test_retry_backoff_only_for_transient_errors():
    calls, waits = [], []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise OSError(errno.ESTALE, "Stale NFS file handle")
        return "ok"

    assert netlib.retry_io(flaky, sleep=waits.append) == "ok" and waits == [0.25, 0.5]
    with pytest.raises(FileNotFoundError):
        netlib.retry_io(lambda: (_ for _ in ()).throw(FileNotFoundError(errno.ENOENT, "gone")), sleep=waits.append)
    assert waits == [0.25, 0.5]  # no retry for a real "not found"
    with pytest.raises(TimeoutError):
        netlib.retry_io(lambda: (_ for _ in ()).throw(TimeoutError()), attempts=3, sleep=lambda s: None)
    assert netlib.probe("/definitely/not/here")[0] is False
    hang = threading.Event()
    value, error = netlib.run_with_timeout(lambda: hang.wait(5), 0.1, "fallback")
    assert value == "fallback" and isinstance(error, TimeoutError)  # a hung share cannot hang the caller
    hang.set()


def test_partially_copied_and_empty_files_are_not_ready(tmp_path):
    empty, growing = tmp_path / "empty.jpg", tmp_path / "growing.jpg"
    empty.write_bytes(b"")
    growing.write_bytes(b"abc")
    assert netlib.stable_size(str(empty)) is None

    def copy_more(_seconds):
        with growing.open("ab") as handle:
            handle.write(b"more")

    assert netlib.stable_size(str(growing), wait=0.01, sleep=copy_more) is None
    assert netlib.stable_size(str(growing), wait=0.01, sleep=lambda s: None) == 7


# -- fixture "share" ----------------------------------------------------------------------------
def make_photo(path: Path, seed: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (96, 72), ((seed * 53) % 255, (seed * 97) % 255, (seed * 29) % 255)).save(path, quality=85)


def fingerprint(root: Path) -> dict:
    """Every path on the share with its bytes, size and mtime: the share must be identical after a session."""
    out = {}
    for path in sorted(root.rglob("*")):
        info = path.lstat()
        out[str(path.relative_to(root))] = (hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "dir", info.st_size if path.is_file() else 0, info.st_mtime_ns)
    return out


@pytest.fixture
def share(client, app_services, tmp_path):
    """A folder standing in for a mounted SMB share, registered as a network library."""
    root = tmp_path / "Volumes" / "Photos"
    names = ["2023/IMG_0001.jpg", "2023/IMG_0002.jpg", "2023/Trip/a.jpg", "2023/Trip/b.JPG", "2024/Café/été.jpg", "2024/x.png", "top.jpg"]
    for index, name in enumerate(names):
        if name.endswith(".png"):
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (40, 40), (index, 9, 9)).save(root / name)
        else:
            make_photo(root / name, index + 1)
    # Things a Windows/NAS share is full of and that must never be indexed.
    for junk in ("$RECYCLE.BIN/S-1-5/$R1.jpg", "System Volume Information/x.jpg", "@eaDir/top.jpg/SYNOPHOTO_THUMB.jpg", "2023/._IMG_0001.jpg",
                 "2023/Thumbs.db", "2023/desktop.ini", ".Trashes/501/old.jpg", "#recycle/gone.jpg", "2024/.DS_Store"):
        (root / junk).parent.mkdir(parents=True, exist_ok=True)
        (root / junk).write_bytes(b"\xff\xd8junk")
    (root / "2024" / "copying.jpg").write_bytes(b"")  # a copy that has only just started
    r = client.post("/api/libraries", json={"path": str(root)})
    assert r.status_code == 200, r.text
    library_id = r.json()["id"]
    with app_services.db.connect() as conn:
        conn.execute("UPDATE libraries SET root_kind='network', share_protocol='smb', share_server='windows-pc', share_name='Photos', share_subpath='' WHERE id=?", (library_id,))
    app_services.libraries.refresh_protection()
    yield app_services, client, root, library_id, len(names)
    netlib.protect([])


@contextmanager
def other_computer():
    """Changes made on the share by its owner (the Windows PC), not by this app: the write guard is ours alone."""
    saved, netlib._protected = netlib._protected, ()
    try:
        yield
    finally:
        netlib._protected = saved


def scan(s, library_id, **options) -> dict:
    s.worker.start(library_id, **options)
    s.worker._thread.join(120)
    job = s.worker.latest()
    stats = json.loads(s.db.one("SELECT last_scan_stats FROM libraries WHERE id=?", (library_id,))["last_scan_stats"] or "null")
    return {"job": job, "stats": stats}


def rows(s, library_id, where="1=1"):
    return s.db.all(f"SELECT * FROM media WHERE library_id=? AND {where} ORDER BY rel_path", (library_id,))


# -- scanning ---------------------------------------------------------------------------------------
def test_full_scan_skips_junk_stores_relative_paths_and_is_incremental(share, monkeypatch):
    s, client, root, lid, expected = share
    first = scan(s, lid)
    assert first["job"]["status"] == "completed", first["job"]
    media = rows(s, lid)
    assert [m["rel_path"] for m in media] == sorted(["2023/IMG_0001.jpg", "2023/IMG_0002.jpg", "2023/Trip/a.jpg", "2023/Trip/b.JPG",
                                                     "2024/Café/été.jpg", "2024/x.png", "top.jpg"])
    assert all(m["status"] == "indexed" and m["missing"] == 0 for m in media) and len(media) == expected
    assert first["stats"]["changed"] == expected and first["stats"]["mode"] == "full"
    # Unchanged share: nothing is opened, nothing re-indexed.
    opened = []
    real_open = open

    def watching(path, *args, **kwargs):
        # Only the scan itself (its thread and its readers); background jobs from the first scan may still be reading.
        if str(path).startswith(str(root)) and threading.current_thread().name.startswith(("lfs-job-", "fh-read")):
            opened.append(str(path))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", watching)
    second = scan(s, lid)
    monkeypatch.undo()
    assert second["stats"]["changed"] == 0 and opened == [] and second["job"]["skipped"] == expected
    # One new file and one changed file: exactly those two are read.
    with other_computer():
        make_photo(root / "2023" / "Trip" / "new.jpg", 77)
        make_photo(root / "top.jpg", 99)
    third = scan(s, lid)
    assert third["stats"]["changed"] == 2 and len(rows(s, lid)) == expected + 1
    # The API shows it as a network library with its share.
    listed = client.get("/api/libraries").json()["items"][0]
    assert listed["kind"] == "network" and listed["share"]["server"] == "windows-pc" and listed["status"] == "online"
    assert listed["media_count"] == expected + 1 and listed["last_scan"]["files"] == expected + 1 and listed["next_scan_at"]


def test_quick_scan_only_lists_folders_that_changed(share, monkeypatch):
    s, _client, root, lid, expected = share
    scan(s, lid)
    listed = []
    real_scandir = os.scandir
    monkeypatch.setattr(netscan.os, "scandir", lambda path: (listed.append(str(path)), real_scandir(path))[1])
    quiet = scan(s, lid, quick=True)
    assert quiet["stats"]["mode"] == "quick" and quiet["stats"]["changed"] == 0
    assert [p for p in listed if p.startswith(str(root) + "/")] == []  # folder times only: no sub-folder is listed
    with other_computer():
        make_photo(root / "2023" / "Trip" / "late.jpg", 5)
    listed.clear()
    changed = scan(s, lid, quick=True)
    assert changed["stats"]["changed"] == 1 and [p for p in listed if p.startswith(str(root) + "/")] == [str(root / "2023" / "Trip")]
    assert len(rows(s, lid)) == expected + 1 and all(m["missing"] == 0 for m in rows(s, lid))


def test_other_spellings_of_the_same_file_never_create_duplicates(share):
    s, _client, root, lid, expected = share
    scan(s, lid)
    target = s.db.one("SELECT * FROM media WHERE rel_path='2024/Café/été.jpg'")
    # The same file as another client spelled it: NFD and different letter case.
    other = str(root) + "/" + unicodedata.normalize("NFD", "2024/CAFÉ/ÉTÉ.JPG")
    with s.db.connect() as conn:
        conn.execute("UPDATE media SET path=? WHERE id=?", (other, target["id"]))
    result = scan(s, lid)
    assert result["job"]["status"] == "completed" and len(rows(s, lid)) == expected
    again = s.db.one("SELECT * FROM media WHERE id=?", (target["id"],))
    assert again["path"] == str(root / "2024" / "Café" / "été.jpg") and again["status"] == "indexed" and again["indexed_at"] == target["indexed_at"]


# -- offline is not missing ------------------------------------------------------------------------
def test_offline_keeps_everything_and_recovers(share, tmp_path):
    s, client, root, lid, expected = share
    scan(s, lid)
    parked = tmp_path / "unplugged"
    with other_computer():
        root.rename(parked)  # the network cable is pulled
    report = s.libraries.tick()
    assert report["offline"] == [lid] and report["scans"] == []
    library = client.get("/api/libraries").json()["items"][0]
    assert library["status"] == "offline" and library["media_count"] == expected and library["missing_count"] == 0
    # Scans refuse politely and change nothing.
    refused = client.post(f"/api/libraries/{lid}/scan")
    assert refused.status_code == 409 and refused.json()["detail"]["offline"] is True
    assert client.post("/api/index", json={"library_id": lid}).status_code == 409
    media = rows(s, lid)
    assert len(media) == expected and all(m["missing"] == 0 and m["deleted_at"] is None and m["status"] == "indexed" for m in media)
    # Still browsable and searchable from local data; items are flagged offline; thumbnails come from the cache.
    listing = client.get("/api/media?limit=50").json()
    assert listing["total"] == expected and all(item["offline"] is True and item["missing"] is False for item in listing["items"])
    assert client.post("/api/search/hybrid", json={"limit": 50}).json()["total"] == expected
    first = media[0]["id"]
    assert client.get(f"/api/media/{first}/thumbnail").status_code == 200
    original = client.get(f"/api/media/{first}/file")
    assert original.status_code == 503 and original.json()["detail"]["offline"] is True and "offline" in original.json()["detail"]["message"]
    # Back again: detected, and a catch-up scan starts by itself.
    with other_computer():
        parked.rename(root)
    back = s.libraries.tick()
    assert back["online"] == [lid] and back["scans"] == [lid]
    s.worker._thread.join(120)
    assert s.worker.latest()["status"] == "completed"
    assert client.get("/api/libraries").json()["items"][0]["status"] == "online"
    assert client.get(f"/api/media/{first}/file").status_code == 200
    assert all(m["missing"] == 0 for m in rows(s, lid)) and len(rows(s, lid)) == expected


def test_share_disappearing_in_the_middle_of_a_scan_marks_nothing_missing(share, tmp_path, monkeypatch):
    s, client, root, lid, expected = share
    scan(s, lid)
    with other_computer():
        make_photo(root / "2023" / "extra.jpg", 31)
    parked = tmp_path / "unplugged"
    real_scandir = os.scandir
    calls = []

    def dying(path):
        calls.append(str(path))
        if len(calls) == 3 and root.exists():
            with other_computer():
                root.rename(parked)  # gone while the scan is walking it
        if not root.exists() and str(path).startswith(str(root)):
            raise OSError(errno.EHOSTDOWN, "Host is down")
        return real_scandir(path)

    monkeypatch.setattr(netscan.os, "scandir", dying)
    monkeypatch.setattr(netlib.os, "scandir", dying)
    monkeypatch.setattr(netlib.time, "sleep", lambda s: None)
    s.worker.start(lid)
    s.worker._thread.join(120)
    job = s.worker.latest()
    assert job["status"] == "offline" and "stopped answering" in job["error"]
    assert s.db.one("SELECT status FROM libraries WHERE id=?", (lid,))["status"] == "offline"
    media = rows(s, lid)
    assert len(media) == expected and all(m["missing"] == 0 and m["missing_checks"] == 0 for m in media)
    monkeypatch.undo()
    with other_computer():
        parked.rename(root)
    s.libraries.tick()
    s.worker._thread.join(120)
    assert s.worker.latest()["status"] == "completed" and len(rows(s, lid)) == expected + 1  # resumed, no duplicates


def test_missing_needs_an_online_listing_twice(share):
    s, client, root, lid, expected = share
    scan(s, lid)
    with other_computer():
        (root / "2023" / "IMG_0002.jpg").unlink()
    scan(s, lid)
    gone = s.db.one("SELECT * FROM media WHERE rel_path='2023/IMG_0002.jpg'")
    assert gone["missing"] == 0 and gone["missing_checks"] == 1  # one listing without it: not yet
    assert client.get("/api/libraries").json()["items"][0]["missing_count"] == 0
    scan(s, lid)
    gone = s.db.one("SELECT * FROM media WHERE rel_path='2023/IMG_0002.jpg'")
    assert gone["missing"] == 1 and gone["deleted_at"] is None  # confirmed, still never deleted
    assert sum(m["missing"] for m in rows(s, lid)) == 1
    # It comes back (same bytes restored from a backup): the mark is cleared without re-indexing.
    with other_computer():
        make_photo(root / "2023" / "IMG_0002.jpg", 2)
        os.utime(root / "2023" / "IMG_0002.jpg", ns=(gone["mtime_ns"], gone["mtime_ns"]))
    result = scan(s, lid)
    back = s.db.one("SELECT * FROM media WHERE id=?", (gone["id"],))
    assert back["missing"] == 0 and back["missing_checks"] == 0 and result["stats"]["changed"] == 0


# -- relink ---------------------------------------------------------------------------------------
def test_relink_after_the_mount_point_changes(share, tmp_path):
    s, client, root, lid, expected = share
    scan(s, lid)
    before = {m["id"]: (m["rel_path"], m["indexed_at"], m["content_hash"]) for m in rows(s, lid)}
    moved = tmp_path / "Volumes" / "Photos-1"
    with other_computer():
        root.rename(moved)  # macOS remounted the share under another name
    s.libraries.tick()
    assert client.get("/api/libraries").json()["items"][0]["status"] == "offline"
    wrong = tmp_path / "somewhere-else"
    make_photo(wrong / "top.jpg", 200)
    preview = client.post(f"/api/libraries/{lid}/relink", json={"path": str(wrong)}).json()
    assert preview["applied"] is False and preview["match_percent"] == 0.0 and preview["checked"] == expected
    preview = client.post(f"/api/libraries/{lid}/relink", json={"path": str(moved)}).json()
    assert preview == {"library_id": lid, "new_root": str(moved.resolve()), "checked": expected, "matched": expected, "match_percent": 100.0, "applied": False}
    assert s.db.one("SELECT path FROM libraries WHERE id=?", (lid,))["path"] == str(root)  # preview changes nothing
    applied = client.post(f"/api/libraries/{lid}/relink", json={"path": str(moved), "apply": True}).json()
    assert applied["applied"] is True
    library = client.get("/api/libraries").json()["items"][0]
    assert library["path"] == str(moved.resolve()) and library["status"] == "online"
    after = rows(s, lid)
    assert {m["id"]: (m["rel_path"], m["indexed_at"], m["content_hash"]) for m in after} == before  # same rows, nothing re-indexed
    assert all(m["path"].startswith(str(moved.resolve())) and Path(m["path"]).is_file() for m in after)
    assert client.get(f"/api/media/{after[0]['id']}/file").status_code == 200
    netlib.protect([str(moved)])
    result = scan(s, lid)
    assert result["stats"]["changed"] == 0 and len(rows(s, lid)) == expected


def test_a_share_remounted_elsewhere_is_followed_automatically(share, tmp_path):
    s, client, root, lid, expected = share
    scan(s, lid)
    moved = tmp_path / "Volumes" / "Photos-1"
    with other_computer():
        root.rename(moved)
    s.libraries.mounts = lambda: [{"source": "//windows-pc/Photos", "mount_point": str(moved), "fstype": "smbfs", "network": True,
                                   "protocol": "smb", "read_only": False, "server": "windows-pc", "share": "Photos"}]
    report = s.libraries.tick()
    assert report["relinked"] == [lid] and report["online"] == [lid]
    assert client.get("/api/libraries").json()["items"][0]["path"] == str(moved.resolve())
    if s.worker._thread is not None:
        s.worker._thread.join(120)
    assert len(rows(s, lid)) == expected and all(m["missing"] == 0 for m in rows(s, lid))


# -- flaky I/O --------------------------------------------------------------------------------------
def test_prefetcher_retries_transient_errors_and_gives_up_on_a_stuck_file(tmp_path):
    files = []
    for index in range(6):
        path = tmp_path / f"f{index}.jpg"
        path.write_bytes(os.urandom(200_000))
        files.append((str(path), "photo"))
    attempts: dict = {}
    release = threading.Event()

    def opener(path, mode):
        attempts[path] = attempts.get(path, 0) + 1
        if path.endswith("f1.jpg") and attempts[path] < 3:
            raise OSError(errno.ETIMEDOUT, "Operation timed out")  # transient: retried
        if path.endswith("f3.jpg"):
            release.wait(10)  # a file that never answers
        if path.endswith("f4.jpg"):
            raise PermissionError(errno.EACCES, "Permission denied")
        time.sleep(0.01)  # network latency
        return open(path, mode)

    prefetch = netscan.Prefetcher(files, concurrency=3, window=4, file_timeout=0.6, opener=opener)
    started = time.perf_counter()
    results = [prefetch.wait(i) for i in range(len(files))]
    elapsed = time.perf_counter() - started
    release.set()
    prefetch.close()
    assert results[0] is None and results[1] is None and results[2] is None and results[5] is None
    assert attempts[files[1][0]] == 3
    assert isinstance(results[3], TimeoutError) and "could not be read within" in str(results[3])
    assert isinstance(results[4], PermissionError)
    assert elapsed < 12 and prefetch.bytes_read == 4 * 200_000 and prefetch.rate() > 0


def test_unreadable_file_is_reported_and_the_scan_continues(share, monkeypatch):
    s, client, root, lid, expected = share
    real = netscan.Prefetcher._read

    def read(self, path, kind):
        if path.endswith("IMG_0002.jpg"):
            raise PermissionError(errno.EACCES, "Permission denied")
        return real(self, path, kind)

    monkeypatch.setattr(netscan.Prefetcher, "_read", read)
    result = scan(s, lid)
    assert result["job"]["status"] == "completed" and result["job"]["failed"] == 1 and result["stats"]["failed"] == 1
    health = client.get(f"/api/libraries/{lid}/health").json()
    assert [u["path"] for u in health["unreadable"]] == ["2023/IMG_0002.jpg"] and "Permission denied" in health["unreadable"][0]["reason"]
    assert str(root) not in json.dumps(health["unreadable"])
    assert health["slowest_folders"] and health["library"]["failed_count"] == 1
    assert len(rows(s, lid, "status='indexed'")) == expected - 1
    monkeypatch.undo()
    assert scan(s, lid)["stats"]["failed"] == 0  # readable again: picked up on the next scan
    test = client.post(f"/api/libraries/{lid}/test").json()
    assert test["online"] is True and test["latency_ms"] >= 0 and test["throughput_mb_per_s"] > 0 and test["free_bytes_local"] > 0


# -- read-only proof --------------------------------------------------------------------------------
def test_the_share_is_byte_identical_after_a_whole_session(share, tmp_path):
    s, client, root, lid, expected = share
    before = fingerprint(root)
    assert scan(s, lid)["job"]["status"] == "completed"
    media = rows(s, lid)
    first, second = media[0]["id"], media[1]["id"]
    # Edits (rating, rotation, crop): sidecars must land in the local data folder, not beside the originals.
    assert client.patch(f"/api/media/{first}/edits", json={"rating": 5, "rotation": 90, "label": "red"}).status_code == 200
    sidecar = s.db.one("SELECT sidecar_path FROM media_edits WHERE media_id=?", (first,))["sidecar_path"]
    assert sidecar.startswith(str(s.config.data_dir)) and not list(root.rglob("*.xmp"))
    assert client.get(f"/api/media/{first}/rendered").status_code == 200
    assert client.get(f"/api/media/{first}/thumbnail").status_code == 200
    assert client.get(f"/api/media/{first}/file").status_code == 200
    # Favourites, an album, an export of copies to a local folder.
    client.post("/api/favorites", json={"media_ids": [first], "favorite": True})
    client.post("/api/albums", json={"name": "From the share", "media_ids": [first, second]})
    exported = client.post("/api/export", json={"media_ids": [first, second]})
    assert exported.status_code == 200
    # Cleanup with "free the space": items are hidden in the app, the files stay where they are.
    cleaned = client.post("/api/storage/cleanup", json={"media_ids": [second], "free_space": True})
    assert cleaned.status_code == 200 and Path(media[1]["path"]).is_file()
    # Anything that tries to change the share is refused at the lowest level.
    for attempt in (lambda: (root / "new.txt").write_text("x"), lambda: os.remove(media[0]["path"]), lambda: os.rename(media[0]["path"], str(root / "r.jpg")),
                    lambda: os.mkdir(root / "made"), lambda: shutil.move(media[0]["path"], str(tmp_path / "stolen.jpg")),
                    lambda: os.utime(media[0]["path"], None), lambda: open(media[0]["path"], "ab")):
        with pytest.raises(ReadOnlyLibrary):
            attempt()
    assert open(media[0]["path"], "rb").read(2) == b"\xff\xd8"  # reading is fine
    scan(s, lid, force=True)
    scan(s, lid, quick=True)
    client.post(f"/api/libraries/{lid}/test")
    # Remove the library: index rows go, originals stay.
    removed = client.request("DELETE", f"/api/libraries/{lid}", json={"confirm": "REMOVE LIBRARY"})
    assert removed.status_code == 200 and client.get("/api/libraries").json()["items"] == []
    assert s.db.one("SELECT COUNT(*) AS n FROM media")["n"] == 0
    assert fingerprint(root) == before
    # Everything the app produced is in its own data folder.
    assert any(Path(s.config.data_dir, "thumbnails").iterdir()) and Path(sidecar).is_file()


# -- folder browser ------------------------------------------------------------------------------
def test_folder_browser_lists_folders_only_and_hides_system_folders(share):
    s, client, root, lid, _ = share
    places = client.get("/api/fs/roots").json()
    assert any(p["name"] == "Home" for p in places["places"]) and "shares" in places and isinstance(places["native_picker"], bool)
    listing = client.get("/api/fs/list", params={"path": str(root), "include_counts": True}).json()
    assert [i["name"] for i in listing["items"]] == ["2023", "2024"]  # no files, no $RECYCLE.BIN, @eaDir, .Trashes ...
    assert listing["parent"] == str(root.parent.resolve()) and listing["breadcrumbs"][-1]["name"] == "Photos"
    by_name = {i["name"]: i for i in listing["items"]}
    assert by_name["2023"]["counts"] == {"photos": 2, "videos": 0} and by_name["2023"]["has_children"] is True and by_name["2023"]["is_readable"] is True
    assert by_name["2024"]["counts"] == {"photos": 2, "videos": 0}  # counted by name (cheap); junk such as .DS_Store is not
    summary = client.get("/api/fs/summary", params={"path": str(root)}).json()
    assert summary["photos"] == 8 and summary["videos"] == 0 and summary["complete"] is True and summary["free_bytes_local"] > 0
    assert summary["already_library"]["id"] == lid
    # Traversal and nonsense.
    assert client.get("/api/fs/list", params={"path": "relative/path"}).status_code == 400
    assert client.get("/api/fs/list", params={"path": str(root / "nope")}).status_code == 404
    assert client.get("/api/fs/list", params={"path": str(root / "top.jpg")}).status_code == 400
    escaped = client.get("/api/fs/list", params={"path": str(root / "2023" / ".." / ".." / "..")}).json()
    assert escaped["path"] == str(root.parent.parent.resolve())  # resolved, not trusted as typed
    # A link out of the folder is reported with where it really points.
    with other_computer():
        (root.parent / "outside").mkdir()
        (root.parent / "linked").symlink_to(root.parent / "outside")
    linked = {i["name"]: i["path"] for i in client.get("/api/fs/list", params={"path": str(root.parent)}).json()["items"]}
    assert linked["linked"] == str((root.parent / "outside").resolve())
    # CSRF header is required even for reading.
    assert client.get("/api/fs/roots", headers={"X-LFS-Request": ""}).status_code == 403


def test_choosing_a_folder_makes_it_an_allowed_root(make_config, tmp_path):
    from fastapi.testclient import TestClient

    from backend.app import create_app
    from backend.services.container import Services
    from tests.conftest import FakeEngine

    inside, outside = tmp_path / "allowed", tmp_path / "elsewhere" / "Photos"
    inside.mkdir()
    outside.mkdir(parents=True)
    config = make_config(allowed_roots=str(inside))
    services = Services(config, engine=FakeEngine())
    try:
        with TestClient(create_app(services=services)) as client:
            client.headers.update({"X-LFS-Request": "1"})
            assert outside.resolve() not in [p for p in config.roots]
            added = client.post("/api/libraries", json={"path": str(outside), "name": "From the chooser"})
            assert added.status_code == 200 and added.json()["name"] == "From the chooser"
            assert client.get("/api/fs/roots").json()["places"][-1]["group"] == "recent"
            assert client.post("/api/libraries", json={"path": str(outside / "..")}).status_code == 400  # overlaps
    finally:
        services.close()
    again = Services(config, engine=FakeEngine())  # persisted: no LFS_ALLOWED_ROOTS editing needed
    try:
        assert outside.resolve() in again.config.roots
    finally:
        again.close()


def test_folder_browser_is_closed_to_other_devices_without_the_app_lock(share, monkeypatch):
    s, client, root, lid, _ = share
    from backend.routers import fs

    monkeypatch.setattr(fs, "is_local_client", lambda request: False)
    for path in ("/api/fs/roots", f"/api/fs/list?path={root}", "/api/shares"):
        assert client.get(path).status_code == 403
    assert client.post("/api/fs/native-pick").status_code == 403
    assert client.post("/api/shares/connect", json={"server": "pc", "share": "x", "guest": True}).status_code == 403
    client.put("/api/lock/password", json={"password": "a long test password"})
    assert client.get("/api/fs/roots").status_code == 200  # signed in: allowed
    assert client.post("/api/fs/native-pick").status_code == 403  # the system dialog stays local-only


# -- connecting shares ------------------------------------------------------------------------------
class FakeRunner:
    def __init__(self):
        self.calls, self.mounted, self.result = [], [], (0, "")

    def __call__(self, command, *, input_text=None, timeout=40.0):
        self.calls.append((command, input_text))
        if command[0].endswith("osascript") and self.result[0] == 0:
            self.mounted.append({"source": "//windows-pc/Photos", "mount_point": "/Volumes/Photos", "fstype": "smbfs", "network": True,
                                 "protocol": "smb", "read_only": False, "server": "windows-pc", "share": "Photos"})
        return self.result


def test_connecting_a_share_never_exposes_the_password(app_services, caplog):
    runner = FakeRunner()
    shares = ShareService(app_services, runner=runner, mounts=lambda: list(runner.mounted), platform="darwin")
    secret = 'p@ss "word" \\ 123'
    with caplog.at_level("DEBUG"):
        mount = shares.connect(server="Windows-PC", share="Photos", username="imtiaj", password=secret, remember=True)
    assert mount["mount_point"] == "/Volumes/Photos" and mount["already_connected"] is False
    command, script = runner.calls[0]
    assert command == ["/usr/bin/osascript", "-"] and secret not in " ".join(command)  # only on stdin
    assert 'mount volume "smb://Windows-PC/Photos" as user name "imtiaj" with password "p@ss \\"word\\" \\\\ 123"' in script
    keychain, keychain_input = runner.calls[1]
    assert keychain == ["/usr/bin/security", "-i"] and "add-internet-password" in keychain_input and secret not in " ".join(keychain)
    assert secret not in caplog.text and "123" not in caplog.text
    assert secret not in json.dumps(app_services.db.all("SELECT * FROM settings")) and secret not in json.dumps(mount)
    assert shares.connect(server="windows-pc", share="photos", guest=True)["already_connected"] is True and len(runner.calls) == 2
    # Guest, and injection attempts in every field.
    runner.mounted.clear()
    shares.connect(server="windows-pc", share="Photos", guest=True)
    assert 'mount volume "smb://guest@windows-pc/Photos"' in runner.calls[-1][1] and "password" not in runner.calls[-1][1]
    for bad in ({"server": 'pc"; do shell script "rm -rf ~', "share": "x"}, {"server": "pc", "share": 'x" & (do shell script "id") & "'},
                {"server": "pc", "share": "../../etc"}, {"server": "pc", "share": "x", "username": 'me"\nquit'}, {"server": "", "share": "x"}):
        calls = len(runner.calls)
        with pytest.raises(ShareError):
            shares.connect(**bad, password="x")
        assert len(runner.calls) == calls  # rejected before anything ran


@pytest.mark.parametrize("output,code,status", [("execution error: Authentication error (-5045)", "auth", 401),
                                                 ("server connection failed: No route to host", "unreachable", 502),
                                                 ("mount error: No such file or directory", "share_not_found", 404)])
def test_connect_failures_reach_the_user_as_next_steps(client, app_services, output, code, status):
    runner = FakeRunner()
    runner.result = (1, output)
    app_services.shares = ShareService(app_services, runner=runner, mounts=lambda: [], platform="darwin")
    r = client.post("/api/shares/connect", json={"server": "windows-pc", "share": "Photos", "username": "me", "password": "wrong"})
    assert r.status_code == status and r.json()["detail"]["code"] == code and r.json()["detail"]["next_step"]
    assert "wrong" not in r.text


def test_shares_connected_in_finder_are_listed_and_busy_shares_are_not_unmounted(share, monkeypatch):
    s, client, root, lid, _ = share
    runner = FakeRunner()
    mounts = [{"source": "//windows-pc/Photos", "mount_point": str(root), "fstype": "smbfs", "network": True, "protocol": "smb",
               "read_only": False, "server": "windows-pc", "share": "Photos"}]
    s.shares = ShareService(s, runner=runner, mounts=lambda: mounts, platform="darwin")
    listed = client.get("/api/shares").json()
    assert listed["items"][0]["mount_point"] == str(root) and listed["items"][0]["libraries"] == [{"id": lid, "name": "Photos"}]
    monkeypatch.setattr(type(s.worker), "busy", property(lambda self: True))
    monkeypatch.setattr(s.worker, "latest", lambda: {"library_id": lid})
    refused = client.post("/api/shares/disconnect", json={"mount_point": str(root)})
    assert refused.status_code == 409 and "scan is reading this share" in refused.json()["detail"]["message"] and runner.calls == []
    monkeypatch.undo()
    assert ShareService.parse_dns_sd(" 1:02:03.456  Add        2  14 local.               _smb._tcp.           Windows PC\n"
                                     " 1:02:03.457  Add        2  14 local.               _smb._tcp.           nas\n") == [
        {"name": "nas", "server": "nas.local", "protocol": "smb"}, {"name": "Windows PC", "server": "Windows-PC.local", "protocol": "smb"}]


def test_permanent_delete_removes_the_file_on_the_share_and_nothing_else(share, monkeypatch):
    """The user's decision: a confirmed permanent delete does delete the original on the share.
    It is the only write the app makes there, and only for the confirmed files."""
    s, client, root, lid, expected = share
    scan(s, lid)
    media = rows(s, lid)
    first, second, third = media[0], media[1], media[2]
    before = fingerprint(root)
    with s.db.connect() as conn:
        conn.executemany("UPDATE media SET deleted_at=CURRENT_TIMESTAMP WHERE id=?", [(first["id"],), (second["id"],)])
    assert client.post("/api/media/purge", json={"media_ids": [first["id"]], "confirm": "nope"}).status_code == 400
    assert Path(first["path"]).is_file()
    result = client.post("/api/media/purge", json={"media_ids": [first["id"]], "confirm": "DELETE"}).json()
    assert result == {"deleted_files": 1, "removed_from_index": 1, "failed": []}
    assert not Path(first["path"]).exists() and s.db.one("SELECT 1 AS x FROM media WHERE id=?", (first["id"],)) is None
    emptied = client.post("/api/media/empty-deleted", json={"confirm": "DELETE"}).json()
    assert emptied["deleted_files"] == 1 and emptied["failed"] == [] and not Path(second["path"]).exists()
    # Everything else on the share is untouched, and every other kind of change is still refused.
    after = fingerprint(root)
    gone = {str(Path(m["path"]).relative_to(root)) for m in (first, second)}
    changed_dirs = {str(Path(m["path"]).parent.relative_to(root)) for m in (first, second)}
    assert set(before) - set(after) == gone
    assert {k: v for k, v in after.items() if k not in changed_dirs} == {k: v for k, v in before.items() if k not in gone and k not in changed_dirs}
    for attempt in (lambda: os.remove(third["path"]), lambda: os.rename(third["path"], str(root / "r.jpg")), lambda: (root / "new.txt").write_text("x")):
        with pytest.raises(ReadOnlyLibrary):
            attempt()
    # If the share refuses (read-only account), the item stays and the reason is shown.
    with s.db.connect() as conn:
        conn.execute("UPDATE media SET deleted_at=CURRENT_TIMESTAMP WHERE id=?", (third["id"],))
    real_unlink = Path.unlink

    def refuse(self, *a, **k):
        if str(self) == third["path"]:
            raise PermissionError(13, "Permission denied")
        return real_unlink(self, *a, **k)

    monkeypatch.setattr(Path, "unlink", refuse)
    refused = client.post("/api/media/purge", json={"media_ids": [third["id"]], "confirm": "DELETE"}).json()
    assert refused["removed_from_index"] == 0 and "Permission denied" in refused["failed"][0]["error"] and Path(third["path"]).is_file()
