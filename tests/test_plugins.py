"""Plugins: isolation (crash, hang), sandbox (files, network, processes), API version, reference plugins."""

import socket
import textwrap
import time
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from backend.plugins import API_VERSION, host
from backend.plugins.manifest import ManifestError, check_api_version, load
from tests.helpers.fixtures import FAKE_TEXT, FAKE_VISUAL, fake_vector
from tests.test_search import FakeTextEncoder

REPO = Path(__file__).resolve().parents[1]
REFERENCE = REPO / "plugins"

SNOOP = '''
import os, time

def rank(query, items):
    kind, _, arg = query.partition(" ")
    if kind == "read":
        return [{"id": items[0]["id"], "score": float(len(open(arg, "rb").read()))}]
    if kind == "list":
        return [{"id": items[0]["id"], "score": float(len(os.listdir(arg)))}]
    if kind == "write":
        open(arg, "w").write("x")
    if kind == "delete":
        os.remove(arg)
    if kind == "stat":
        return [{"id": items[0]["id"], "score": float(os.stat(arg).st_size)}]
    if kind == "net":
        import socket
        socket.create_connection(("127.0.0.1", int(arg)), timeout=2).close()
    if kind == "dns":
        import socket
        socket.getaddrinfo("example.com", 80)
    if kind == "proc":
        import subprocess
        subprocess.run(["true"])
    if kind == "native":
        import ctypes
        ctypes.CDLL(arg)
    if kind == "crash":
        os._exit(7)
    if kind == "hang":
        time.sleep(60)
    if kind == "flood":
        print("x" * 100000)
    if kind == "garbage":
        os.write(1, b"not json\\n")
    return [{"id": i["id"], "score": 1.0 if "7" in i["name"] else 0.1} for i in items]

def plan(items, options):
    out = [{"id": i["id"], "path": "ok/" + i["name"]} for i in items]
    first = items[0]["id"]
    return out + [{"id": first, "path": "../escape.jpg"}, {"id": first, "path": "/tmp/fh-absolute.jpg"},
                  {"id": first, "path": "a/../../b.jpg"}, {"id": 999999, "path": "ghost.jpg"}, {"id": first, "path": ""}]

def classify(images):
    return [[{"label": "Beach", "score": 0.9}, {"label": "x" * 500, "score": float("nan")}] for _ in images]
'''


def write_plugin(folder: Path, *, plugin_id="snoop", permissions=(), api="1.0", code=SNOOP, extra="") -> Path:
    folder.mkdir(parents=True)
    (folder / "plugin.py").write_text(code)
    perms = ", ".join(f'"{p}"' for p in permissions)
    (folder / "plugin.toml").write_text(textwrap.dedent(f"""
        [plugin]
        id = "{plugin_id}"
        name = "Snoop"
        version = "0.1.0"
        api_version = "{api}"
        permissions = [{perms}]
        [capabilities.search_signal]
        entry = "plugin:rank"
        [capabilities.export]
        entry = "plugin:plan"
        [capabilities.classifier]
        entry = "plugin:classify"
        """) + extra)
    return folder


@pytest.fixture
def library(client, app_services, tmp_path):
    """Twelve real photos (two people) in a library folder, with text and visual vectors."""
    s = app_services
    lib = tmp_path / "lib"
    lib.mkdir()
    rng = np.random.default_rng(3)
    with s.db.connect() as conn:
        conn.execute("INSERT INTO libraries(id, path, name) VALUES (1, ?, 'lib')", (str(lib),))
        conn.execute("INSERT INTO people(id, name, face_count) VALUES (1,'Ada Lovelace',6),(2,NULL,3)")
        for i in range(1, 13):
            path = lib / f"img_{i}.jpg"
            colour = (255, 40, 40) if i <= 6 else (40, 40, 255)
            pixels = np.clip(np.array(colour) + rng.integers(-25, 25, (48, 64, 3)), 0, 255).astype(np.uint8)
            Image.fromarray(pixels).save(path, quality=90)
            conn.execute("INSERT INTO media(id, library_id, path, name, kind, size, mtime_ns, captured_at, status, width, height)"
                         " VALUES (?,1,?,?,'photo',?,1,?,'indexed',64,48)",
                         (i, str(path), path.name, path.stat().st_size, f"202{i % 3}-05-0{i % 9 + 1}T10:00:00"))
            person = 1 if i <= 6 else 2 if i <= 9 else None
            if person:
                offset, sha = s.store.append(fake_vector(i, 512))
                conn.execute("INSERT INTO faces(media_id, person_id, bbox, detection, embedding_offset, embedding_sha)"
                             " VALUES (?,?,'[0,0,10,10]',0.9,?,?)", (i, person, offset, sha))
    ids = list(range(1, 13))
    s.vectors.register(FAKE_TEXT).add([(i, fake_vector(i, FAKE_TEXT.dim)) for i in ids])
    s.vectors.register(FAKE_VISUAL).add([(i, fake_vector(i, FAKE_VISUAL.dim, salt=5)) for i in ids])
    (lib / "secret.txt").write_text("the user's private note")
    return s, lib


def install(client, folder, *, enable=True, permissions=()):
    r = client.post("/api/plugins/install", json={"path": str(folder)})
    assert r.status_code == 200, r.text
    plugin = r.json()
    assert plugin["enabled"] is False and plugin["granted_permissions"] == []  # nothing runs, nothing granted by default
    if enable:
        r = client.patch(f"/api/plugins/{plugin['id']}", json={"enabled": True, "permissions": list(permissions)})
        assert r.status_code == 200, r.text
        plugin = r.json()
    return plugin


def rank(s, query):
    return s.plugins.call("snoop", "search_signal.rank", {"query": query, "items": [{"id": 1, "name": "img_1.jpg"}]}, timeout=10)


# -- manifest and API version -----------------------------------------------------------------
def test_api_version_mismatch_is_rejected_with_a_clear_message(client, tmp_path):
    check_api_version(API_VERSION)
    for wanted in ("2.0", "1.9", "0.9"):
        with pytest.raises(ManifestError, match=f"needs plugin API {wanted}; this app provides {API_VERSION}"):
            check_api_version(wanted)
    with pytest.raises(ManifestError, match="must look like"):
        check_api_version("one")
    r = client.post("/api/plugins/install", json={"path": str(write_plugin(tmp_path / "future", api="2.0"))})
    assert r.status_code == 400 and "needs plugin API 2.0; this app provides 1.0" in r.json()["detail"]
    assert client.get("/api/plugins").json()["items"] == []


def test_manifest_validation(tmp_path):
    with pytest.raises(ManifestError, match="no plugin.toml"):
        load(tmp_path)
    with pytest.raises(ManifestError, match="unknown permission"):
        load(write_plugin(tmp_path / "a", permissions=["root"]))
    bad = write_plugin(tmp_path / "b")
    (bad / "link").symlink_to("/etc/hosts")
    with pytest.raises(ManifestError, match="symbolic links"):
        load(bad)
    with pytest.raises(ManifestError, match="unknown capability"):
        load(write_plugin(tmp_path / "c", extra='[capabilities.kernel]\nentry = "plugin:x"\n'))
    with pytest.raises(ManifestError, match="not in the plugin folder"):
        load(write_plugin(tmp_path / "d", extra='[capabilities.embedding]\nentry = "missing:x"\ndim = 4\nmodel_id = "m"\n'))
    with pytest.raises(ManifestError, match="relative .html"):
        load(write_plugin(tmp_path / "e", extra='[capabilities.ui_panel]\nentry = "../../index.html"\n'))
    with pytest.raises(ManifestError, match="id must be"):
        load(write_plugin(tmp_path / "f", plugin_id="../evil"))


# -- isolation ------------------------------------------------------------------------------------
def test_a_crashing_or_hanging_plugin_never_takes_the_server_down(client, library, tmp_path, monkeypatch):
    s, _lib = library
    monkeypatch.setattr(s.models, "text_encoder", lambda: FakeTextEncoder())
    install(client, write_plugin(tmp_path / "snoop"))
    ok = client.post("/api/search/hybrid", json={"text": "photo 7"}).json()
    assert "plugin_snoop" in ok["signals"] and ok["items"][0]["id"] == 7 and ok["warnings"] == []
    first_pid = s.plugins._processes["snoop"].pid
    # The plugin kills its own process in the middle of a search: the search still answers.
    crashed = client.post("/api/search/hybrid", json={"text": "crash 7"})
    assert crashed.status_code == 200 and crashed.json()["items"]
    assert "ended unexpectedly" in crashed.json()["warnings"][0] and "exit code 7" in crashed.json()["warnings"][0]
    assert client.get("/api/health").status_code == 200
    # Next call gets a fresh process.
    again = client.post("/api/search/hybrid", json={"text": "photo 7"}).json()
    assert "plugin_snoop" in again["signals"] and s.plugins._processes["snoop"].pid != first_pid
    # Writing to stdout cannot reach the reply channel (it is private), and a hang is killed at the 2 s budget.
    assert client.post("/api/search/hybrid", json={"text": "garbage 7"}).json()["warnings"] == []
    assert "ended unexpectedly" in client.post("/api/search/hybrid", json={"text": "crash 7"}).json()["warnings"][0]
    started = time.perf_counter()
    hung = client.post("/api/search/hybrid", json={"text": "hang 7"}).json()
    assert time.perf_counter() - started < 6 and "did not answer" in hung["warnings"][0] and hung["items"]
    # Three crashes: switched off, with the reason kept for the plugin manager.
    plugin = next(p for p in client.get("/api/plugins").json()["items"] if p["id"] == "snoop")
    assert plugin["enabled"] is False and "Disabled after 3 crashes" in plugin["last_error"]
    quiet = client.post("/api/search/hybrid", json={"text": "photo 7"}).json()
    assert "plugin_snoop" not in quiet["signals"] and quiet["warnings"] == []


def test_printing_does_not_corrupt_the_channel(client, library, tmp_path):
    s, _ = library
    install(client, write_plugin(tmp_path / "snoop"))
    assert rank(s, "flood")[0]["id"] == 1
    assert "xxxx" in (s.plugins.root / "snoop.log").read_text()


# -- sandbox -------------------------------------------------------------------------------------
@pytest.mark.parametrize("kernel", [True, False], ids=["kernel+audit", "audit-only"])
def test_without_permissions_a_plugin_cannot_read_files_or_use_the_network(client, library, tmp_path, monkeypatch, kernel):
    s, lib = library
    if kernel and not host.kernel_sandbox_available():
        pytest.skip("no kernel sandbox on this platform")
    monkeypatch.setattr(host, "_kernel_sandbox", kernel)
    install(client, write_plugin(tmp_path / "snoop", permissions=["files.read", "network", "storage"]))  # asked for, not granted
    secret, photo, database = lib / "secret.txt", lib / "img_1.jpg", Path(s.config.data_dir) / "index.sqlite"
    outside = tmp_path / "outside.txt"
    outside.write_text("keep")
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(0.5)
    try:
        for query in (f"read {secret}", f"read {photo}", f"read {database}", f"list {lib}", f"read {Path.home()}/.ssh/id_rsa",
                      f"write {tmp_path / 'made.txt'}", f"write {s.plugins.root / 'snoop' / 'plugin.py'}", f"delete {outside}",
                      f"net {listener.getsockname()[1]}", "dns x", "proc x", "native libc.dylib"):
            with pytest.raises(host.PluginError) as denied:
                rank(s, query)
            assert denied.value.denied, (query, str(denied.value))
        with pytest.raises(socket.timeout):
            listener.accept()  # nothing ever reached the socket
        assert not (tmp_path / "made.txt").exists() and outside.read_text() == "keep"
        assert SNOOP in (s.plugins.root / "snoop" / "plugin.py").read_text() or (s.plugins.root / "snoop" / "plugin.py").read_text() == SNOOP
        if kernel:
            # Not covered by the audit hook (stat): the kernel profile still hides the library and the app's data.
            for path in (secret, database):
                with pytest.raises(host.PluginError, match="PermissionError|Operation not permitted"):
                    rank(s, f"stat {path}")
        assert s.plugins._processes["snoop"].kernel_sandbox is kernel
        # Granting is explicit and per permission.
        r = client.patch("/api/plugins/snoop", json={"permissions": ["files.read", "network", "storage", "library.read"]})
        assert r.status_code == 400 and "did not ask for: library.read" in r.json()["detail"]
        client.patch("/api/plugins/snoop", json={"permissions": ["files.read"]})
        assert rank(s, f"read {secret}")[0]["score"] == len("the user's private note")
        with pytest.raises(host.PluginError):
            rank(s, f"read {database}")           # the app's own data is never readable
        with pytest.raises(host.PluginError):
            rank(s, f"net {listener.getsockname()[1]}")
        with pytest.raises(host.PluginError):
            rank(s, f"write {lib / 'new.txt'}")   # files.read is read-only
        client.patch("/api/plugins/snoop", json={"permissions": ["network", "storage"]})
        rank(s, f"net {listener.getsockname()[1]}")
        listener.accept()[0].close()
        own = s.plugins.storage_root / "snoop" / "notes.txt"
        rank(s, f"write {own}")
        assert own.read_text() == "x"
        with pytest.raises(host.PluginError):
            rank(s, f"read {secret}")
    finally:
        listener.close()


def test_the_plugin_environment_carries_no_server_secrets(client, library, tmp_path, monkeypatch):
    s, _ = library
    monkeypatch.setenv("LFS_APP_PASSWORD", "hunter2")
    code = "import os\ndef rank(query, items):\n    return [{'id': 1, 'score': float('LFS_APP_PASSWORD' in os.environ)}]\n" \
           "def plan(items, options):\n    return []\ndef classify(images):\n    return []\n"
    install(client, write_plugin(tmp_path / "snoop", code=code))
    assert rank(s, "x")[0]["score"] == 0.0


# -- export target: the host validates every path ----------------------------------------------------
def test_export_plans_cannot_escape_the_target_folder(client, library, tmp_path):
    s, lib = library
    install(client, write_plugin(tmp_path / "snoop"))
    target = tmp_path / "out" / "deep"
    job = client.post("/api/plugins/snoop/export", json={"media_ids": [1, 2, 3], "target_dir": str(target)}).json()
    done = s.jobs.wait(job["id"], timeout=60)
    assert done["status"] == "completed", done
    written = sorted(p.relative_to(target).as_posix() for p in target.rglob("*") if p.is_file())
    assert written == ["ok/img_1.jpg", "ok/img_2.jpg", "ok/img_3.jpg"]
    assert not (tmp_path / "out" / "escape.jpg").exists() and not Path("/tmp/fh-absolute.jpg").exists()
    assert (target / "ok/img_1.jpg").read_bytes() == (lib / "img_1.jpg").read_bytes()
    # Running again never overwrites.
    again = s.jobs.wait(client.post("/api/plugins/snoop/export", json={"media_ids": [1], "target_dir": str(target)}).json()["id"], timeout=60)
    assert again["status"] == "completed" and (target / "ok/img_1-1.jpg").is_file()
    # Never into a library or the app's data folder.
    inside = s.jobs.wait(client.post("/api/plugins/snoop/export", json={"media_ids": [1], "target_dir": str(lib / "x")}).json()["id"], timeout=60)
    assert inside["status"] == "failed" and not (lib / "x").exists()


def test_classifier_labels_become_a_search_signal(client, library, tmp_path, monkeypatch):
    s, _ = library
    monkeypatch.setattr(s.models, "text_encoder", lambda: FakeTextEncoder())
    install(client, write_plugin(tmp_path / "snoop"))
    done = s.jobs.wait(client.post("/api/plugins/snoop/classify").json()["id"], timeout=60)
    assert done["status"] == "completed", done
    assert client.get("/api/media/3/plugin-labels").json()["items"] == [{"plugin_id": "snoop", "label": "beach", "score": 0.9}]
    assert s.plugins.classify("snoop")["processed"] == 0  # incremental
    result = client.post("/api/search/hybrid", json={"text": "beach"}).json()
    assert "label" in result["signals"] and result["items"][0]["signals"]["label"]["similarity"] == 0.9


# -- reference plugins ---------------------------------------------------------------------------------
def test_reference_plugins_install_run_and_uninstall_cleanly(client, library, tmp_path):
    s, lib = library
    for folder in ("color-histogram-embedding", "export-by-person"):
        assert load(REFERENCE / folder).api_version == API_VERSION
    # 1. Custom embedding model, with no permissions at all.
    embedding = install(client, REFERENCE / "color-histogram-embedding")
    assert embedding["requested_permissions"] == [] and embedding["capabilities"]["embedding"]["dim"] == 80
    key = "plugin-color-histogram-embedding-rgb-hist@1:80"
    option = next(o for o in client.get("/api/models/upgrade").json()["options"] if o["key"] == key)
    assert option["active"] is False and option.get("can_embed") is not False
    assert client.post("/api/models/upgrade", json={"to_key": key}).status_code == 200
    deadline = time.time() + 60
    while client.get("/api/models/upgrade").json()["upgrade"]["ready"] is not True:
        assert time.time() < deadline, client.get("/api/models/upgrade").json()
        time.sleep(0.2)
    space = s.vectors.get(key)
    assert space.coverage(max_age=0)["filled"] == 12
    red, other_red, blue = space.vector(1), space.vector(2), space.vector(8)
    assert float(red @ other_red) > 0.95 > float(red @ blue)  # it really looked at the pixels
    assert client.post("/api/models/upgrade/switch").status_code == 200
    similar = client.post("/api/search/hybrid", json={"similar_media_id": 1, "limit": 5}).json()
    assert {i["id"] for i in similar["items"]} <= {2, 3, 4, 5, 6}

    # 2. Export by person: names only with the permission.
    export = install(client, REFERENCE / "export-by-person", permissions=[])
    assert export["requested_permissions"] == ["library.read"] and export["granted_permissions"] == []
    out = tmp_path / "by-person"
    done = s.jobs.wait(client.post("/api/plugins/export-by-person/export", json={"media_ids": list(range(1, 13)), "target_dir": str(out)}).json()["id"], timeout=60)
    assert done["status"] == "completed", done
    assert sorted(p.name for p in out.iterdir()) == ["No people", "person-1", "person-2"]
    client.patch("/api/plugins/export-by-person", json={"permissions": ["library.read"]})
    named = tmp_path / "named"
    done = s.jobs.wait(client.post("/api/plugins/export-by-person/export", json={"media_ids": list(range(1, 13)), "target_dir": str(named)}).json()["id"], timeout=60)
    assert sorted(p.name for p in named.iterdir()) == ["Ada Lovelace", "No people", "person-2"]
    files = [p for p in named.rglob("*") if p.is_file()]
    assert len(files) == 12 and all(p.parent.name in ("2020", "2021", "2022") for p in files)
    assert sorted(p.name for p in lib.iterdir()) == sorted([f"img_{i}.jpg" for i in range(1, 13)] + ["secret.txt"])  # originals untouched

    # Its panel: served only with a locked-down policy; the message API checks permissions on the server.
    page = client.get("/api/plugins/export-by-person/panel/")
    assert page.status_code == 200 and "connect-src 'none'" in page.headers["content-security-policy"]
    assert "sandbox allow-scripts" in page.headers["content-security-policy"]
    for path in ("../plugin.py", "..%2Fplugin.py", "../plugin.toml", "%2e%2e/%2e%2e/index.sqlite"):
        assert client.get(f"/api/plugins/export-by-person/panel/{path}").status_code == 404
    assert client.post("/api/plugins/export-by-person/panel-rpc", json={"method": "library.summary"}).json() == {"photos": 12, "videos": 0, "people": 2}
    assert client.post("/api/plugins/export-by-person/panel-rpc", json={"method": "library.people"}).json()["items"][0]["name"] == "Ada Lovelace"
    client.patch("/api/plugins/export-by-person", json={"permissions": []})
    assert client.post("/api/plugins/export-by-person/panel-rpc", json={"method": "library.people"}).status_code == 403
    assert client.post("/api/plugins/export-by-person/panel-rpc", json={"method": "db.dump"}).status_code == 400

    # 3. Uninstall: processes stopped, files and rows gone, search falls back to the built-in model.
    pids = [p.pid for p in s.plugins._processes.values() if p.pid]
    for plugin_id in ("color-histogram-embedding", "export-by-person"):
        assert client.delete(f"/api/plugins/{plugin_id}").status_code == 200
    assert client.get("/api/plugins").json()["items"] == [] and not any(s.plugins.root.glob("*/")) and s.plugins._processes == {}
    for pid in pids:
        with pytest.raises(ProcessLookupError):
            for _ in range(50):
                import os
                os.kill(pid, 0)
                time.sleep(0.05)
    assert s.vectors.active("visual").key == FAKE_VISUAL.key and key not in s.extra_embedders
    assert client.post("/api/search/hybrid", json={"similar_media_id": 1, "limit": 5}).json()["items"]
    assert client.get("/api/plugins/export-by-person/panel/").status_code == 404
    # Installing again works (nothing was left behind).
    assert install(client, REFERENCE / "export-by-person", enable=False)["enabled"] is False


def test_enabled_plugins_come_back_after_a_restart(make_config, tmp_path):
    from backend.services.container import Services
    from tests.conftest import FakeEngine

    config = make_config()
    first = Services(config, engine=FakeEngine())
    try:
        first.plugins.install(str(REFERENCE / "color-histogram-embedding"))
        first.plugins.configure("color-histogram-embedding", enabled=True)
    finally:
        first.close()
    second = Services(config, engine=FakeEngine())
    try:
        second.plugins.activate()
        assert "plugin-color-histogram-embedding-rgb-hist@1:80" in second.extra_embedders
        assert second.plugins._processes == {}  # nothing is started until it is used
    finally:
        second.close()
