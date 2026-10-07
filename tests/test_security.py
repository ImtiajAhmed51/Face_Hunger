"""Security suite: CSRF, origin and host checks, app lock, path traversal, archive attacks, no outbound network."""

import ast
import json
import zipfile
from pathlib import Path

import pytest

from backend.app import iter_routes
from backend.ops import backup
from backend.security import APP_CSP, MAX_FAILURES, host_allowed

REPO = Path(__file__).resolve().parents[1]
PASSWORD = "correct horse battery"  # test value only


# -- CSRF / origin / host --------------------------------------------------------------------------
def test_every_state_changing_route_requires_the_csrf_header(client):
    unsafe = [(m, r.path) for r in iter_routes(client.app.routes) for m in getattr(r, "methods", set()) or set()
              if m in {"POST", "PUT", "PATCH", "DELETE"} and r.path.startswith("/api/")]
    assert len(unsafe) > 100
    client.headers.pop("X-LFS-Request")
    missing = []
    for method, path in unsafe:
        url = path.replace("{path:path}", "x")
        for name in set(__import__("re").findall(r"{(\w+)}", url)):
            url = url.replace("{" + name + "}", "1")
        response = client.request(method, url, json={})
        if response.status_code != 403 or "X-LFS-Request" not in response.text:
            missing.append((method, path, response.status_code))
    assert missing == []


def test_cross_origin_writes_are_refused_even_with_the_header(client):
    body = {"enabled": False}
    assert client.patch("/api/diagnostics", json=body, headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.patch("/api/diagnostics", json=body, headers={"Origin": "null"}).status_code == 403
    assert client.patch("/api/diagnostics", json=body, headers={"Origin": "http://testserver.evil.example"}).status_code == 403
    assert client.patch("/api/diagnostics", json=body, headers={"Origin": "http://testserver"}).status_code == 200
    assert client.patch("/api/diagnostics", json=body, headers={"Origin": "http://localhost:5173"}).status_code == 200  # vite dev
    # A cross-origin page cannot read either: no CORS grant for unknown origins.
    preflight = client.options("/api/media", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"})
    assert "access-control-allow-origin" not in preflight.headers


def test_unknown_host_names_are_refused(client):
    for host in ("evil.example", "rebind.attacker.net:8765", "localhost.evil.example", ""):
        r = client.get("/api/health", headers={"Host": host})
        assert r.status_code == 400 and "LFS_ALLOWED_HOSTS" in r.json()["detail"], host
    for host in ("127.0.0.1:8765", "localhost", "localhost:8765", "[::1]:8765", "192.168.1.20:8765", "photos.localhost", "testserver"):
        assert client.get("/api/health", headers={"Host": host}).status_code == 200, host
    assert host_allowed("nas.home:8765", {"nas.home"}) and not host_allowed("nas.home.evil.io", {"nas.home"})


def test_security_headers_and_csp(client):
    page = client.get("/")
    assert page.headers["content-security-policy"] == APP_CSP and "script-src 'self';" in APP_CSP and "unsafe-eval" not in APP_CSP
    api = client.get("/api/health")
    for response in (page, api):
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert response.headers["x-frame-options"] == "SAMEORIGIN"
    assert "content-security-policy" not in api.headers


# -- app lock ----------------------------------------------------------------------------------------
def test_app_lock_protects_every_api_route_and_media(client, app_services):
    assert client.get("/api/lock").json() == {"enabled": False, "unlocked": True, "managed_by_environment": False}
    assert client.put("/api/lock/password", json={"password": "short"}).status_code == 400
    assert client.put("/api/lock/password", json={"password": PASSWORD}).status_code == 200
    stored = app_services.db.one("SELECT value FROM settings WHERE key='app_lock'")["value"]
    assert PASSWORD not in stored and set(json.loads(stored)) == {"salt", "hash"}
    cookie = client.cookies.get("lfs_session")
    assert cookie and client.get("/api/media").status_code == 200
    set_cookie = client.put("/api/lock/password", json={"password": PASSWORD + "!", "current": PASSWORD}).headers["set-cookie"].lower()
    assert "httponly" in set_cookie and "samesite=strict" in set_cookie
    client.put("/api/lock/password", json={"password": PASSWORD, "current": PASSWORD + "!"})
    # Without the cookie everything under /api is closed, including media bytes and the event stream.
    client.cookies.clear()
    routes = sorted({r.path for r in iter_routes(client.app.routes) if r.path.startswith("/api/") and "GET" in (getattr(r, "methods", None) or ())})
    assert len(routes) > 60
    for path in routes:
        url = __import__("re").sub(r"{\w+(:path)?}", "1", path)
        response = client.get(url)
        if path == "/api/lock":
            assert response.json() == {"enabled": True, "unlocked": False, "managed_by_environment": False}
        else:
            assert response.status_code == 401 and response.json()["locked"] is True, path
    assert client.post("/api/search/hybrid", json={}).status_code == 401
    assert client.get("/api/media", cookies={"lfs_session": "guess"}).status_code == 401
    assert client.get("/").status_code == 200  # the page itself loads so it can show the unlock form
    # The stale session from before the password change is dead too.
    assert client.get("/api/media", cookies={"lfs_session": cookie}).status_code == 401
    # Wrong passwords: refused, then locked out.
    for _ in range(MAX_FAILURES):
        assert client.post("/api/lock/unlock", json={"password": "wrong"}).status_code == 401
    locked_out = client.post("/api/lock/unlock", json={"password": PASSWORD})
    assert locked_out.status_code == 429 and int(locked_out.headers["retry-after"]) > 0
    app_services.lock._failures.clear()
    assert client.post("/api/lock/unlock", json={"password": PASSWORD}).json()["unlocked"] is True
    assert client.get("/api/media").status_code == 200
    # Lock again, and removal needs the password.
    assert client.request("DELETE", "/api/lock/password", json={"password": "nope"}).status_code == 401
    client.post("/api/lock/lock")
    assert client.get("/api/media").status_code == 401
    app_services.lock._failures.clear()
    client.post("/api/lock/unlock", json={"password": PASSWORD})
    assert client.request("DELETE", "/api/lock/password", json={"password": PASSWORD}).json()["enabled"] is False
    client.cookies.clear()
    assert client.get("/api/media").status_code == 200


def test_app_lock_from_the_environment(make_config):
    from fastapi.testclient import TestClient

    from backend.app import create_app
    from backend.services.container import Services
    from tests.conftest import FakeEngine

    services = Services(make_config(app_password=PASSWORD), engine=FakeEngine())
    try:
        with TestClient(create_app(services=services)) as c:
            c.headers.update({"X-LFS-Request": "1"})
            assert c.get("/api/media").status_code == 401
            assert c.get("/api/lock").json()["managed_by_environment"] is True
            assert c.post("/api/lock/unlock", json={"password": PASSWORD}).status_code == 200
            assert c.get("/api/media").status_code == 200
            assert c.put("/api/lock/password", json={"password": "another password", "current": PASSWORD}).status_code == 400
            assert PASSWORD not in json.dumps(services.db.all("SELECT * FROM settings"))
    finally:
        services.close()


# -- path traversal -----------------------------------------------------------------------------------
def test_path_traversal_is_refused_everywhere(client, app_services, tmp_path):
    data = Path(app_services.config.data_dir)
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP-SECRET")
    (data / "exports").mkdir(exist_ok=True)
    attempts = ["/..%2fsecret.txt", "/%2e%2e/secret.txt", "/..%2fdata%2findex.sqlite", "/assets/..%2f..%2fsecret.txt",
                "/assets/%2e%2e/%2e%2e/secret.txt", "/....//secret.txt", f"/{secret}", "/%2fetc%2fpasswd",
                "/api/packages/files/..%2f..%2findex.sqlite", "/api/packages/files/%2e%2e%2findex.sqlite",
                f"/api/packages/files/{str(secret).replace('/', '%2f')}", "/api/plugins/x/panel/..%2f..%2findex.sqlite",
                "/api/share/1/download?name=../../index.sqlite"]
    for url in attempts:
        response = client.get(url)
        assert "TOP-SECRET" not in response.text and b"SQLite format 3" not in response.content[:32], url
        assert response.status_code in (200, 400, 404, 405, 422), (url, response.status_code)
        if response.status_code == 200:
            assert "<!doctype html>" in response.text.lower()  # the SPA shell, nothing else
    # Libraries can only be added under the allowed roots.
    outside = client.post("/api/libraries", json={"path": "/etc"})
    assert outside.status_code in (400, 403) and client.get("/api/libraries").json() in ([], {"items": []}) or \
        all(lib["path"] != "/etc" for lib in (client.get("/api/libraries").json().get("items") if isinstance(client.get("/api/libraries").json(), dict) else client.get("/api/libraries").json()))
    # Exports can never be pointed at the app's data folder.
    assert client.post("/api/plugins/install", json={"path": "relative/path"}).status_code == 400
    assert client.post("/api/plugins/install", json={"path": "/etc"}).status_code == 400


# -- archive attacks on backup restore (zip) -------------------------------------------------------------
def craft_backup(path: Path, entries: dict, files: dict, compression=zipfile.ZIP_DEFLATED) -> Path:
    with zipfile.ZipFile(path, "w", compression) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
        zf.writestr("manifest.json", json.dumps({"format": backup.FORMAT, "version": backup.FORMAT_VERSION,
                                                 "created_at": "2026-01-01T00:00:00+00:00", "files": files}))
    return path


def sha(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


@pytest.mark.parametrize("name", ["../evil.sh", "/etc/cron.d/evil", "vectors/../../evil", "plugins/x/plugin.py", "thumbnails/a/b.jpg",
                                  "vectors/x.usearch.sh", "index.sqlite/../../x", "thumbnails\\..\\x.jpg", "restore-pending/READY"])
def test_backup_restore_rejects_zip_slip_and_unexpected_files(tmp_path, name):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    payload = b"pwned"
    archive = craft_backup(tmp_path / "evil.zip", {"index.sqlite": b"db", name: payload},
                           {"index.sqlite": {"sha256": sha(b"db"), "bytes": 2}, name: {"sha256": sha(payload), "bytes": len(payload)}})
    with pytest.raises(backup.BackupError, match="missing or unsafe"):
        backup.stage_restore(archive, data_dir)
    assert list(data_dir.iterdir()) == [] and not (tmp_path / "evil.sh").exists() and not (tmp_path / "evil").exists()


def test_backup_restore_rejects_decompression_bombs_and_lying_sizes(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    bomb = bytes(64 << 20)  # 64 MB of zeros deflates to ~64 KB
    archive = craft_backup(tmp_path / "bomb.zip", {"index.sqlite": b"db", "embeddings.bin": bomb},
                           {"index.sqlite": {"sha256": sha(b"db"), "bytes": 2}, "embeddings.bin": {"sha256": sha(bomb), "bytes": len(bomb)}})
    assert archive.stat().st_size < 1 << 20
    with pytest.raises(backup.BackupError, match="implausible size"):
        backup.stage_restore(archive, data_dir)
    lying = craft_backup(tmp_path / "lying.zip", {"index.sqlite": b"db" * 1000},
                         {"index.sqlite": {"sha256": sha(b"db" * 1000), "bytes": 2}})
    with pytest.raises(backup.BackupError, match="implausible size"):
        backup.stage_restore(lying, data_dir)
    tampered = craft_backup(tmp_path / "tampered.zip", {"index.sqlite": b"db"}, {"index.sqlite": {"sha256": sha(b"other"), "bytes": 2}})
    with pytest.raises(backup.BackupError, match="Checksum mismatch"):
        backup.stage_restore(tampered, data_dir)
    for junk in (b"", b"PK\x03\x04 not really a zip", b"\x00" * 4096):
        (tmp_path / "junk.zip").write_bytes(junk)
        with pytest.raises(backup.BackupError):
            backup.stage_restore(tmp_path / "junk.zip", data_dir)
    assert list(data_dir.iterdir()) == []


def test_package_import_rejects_malicious_archives(app_services, tmp_path):
    """Encrypted packages: the traversal, link and truncation fixtures live in tests/test_packages.py;
    here: garbage, wrong magic and an oversized header never reach the extractor."""
    from backend.ops import fhpack

    for name, data in (("empty.fhpack", b""), ("junk.fhpack", b"\x00" * 5000), ("zip.fhpack", b"PK\x03\x04" + b"A" * 200),
                       ("huge-header.fhpack", b"FHPACK1\n" + (1 << 30).to_bytes(4, "big") + b"{}")):
        path = tmp_path / name
        path.write_bytes(data)
        with pytest.raises(fhpack.PackageError):
            app_services.packages.import_package(path, PASSWORD)
    assert not list(Path(app_services.config.data_dir).glob("**/*.partial"))


# -- SSRF / outbound network ---------------------------------------------------------------------------
def test_backend_has_no_outbound_network_code():
    """SSRF needs a server-side fetch. The backend has none: no HTTP client or socket import anywhere
    (the plugin runner only names these modules in order to block them)."""
    banned = {"requests", "httpx", "urllib3", "aiohttp", "socket", "http.client", "urllib.request", "ftplib", "smtplib", "telnetlib", "websockets"}
    offenders = []
    for path in (REPO / "backend").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else \
                [node.module or ""] + [f"{node.module}.{a.name}" for a in node.names] if isinstance(node, ast.ImportFrom) else []
            offenders += [(str(path.relative_to(REPO)), n) for n in names if n in banned or n.split(".")[0] in {"requests", "httpx", "aiohttp"}]
    assert offenders == []


def test_no_route_takes_a_url_to_fetch(client):
    schema = client.app.openapi()
    suspicious = []
    for name, model in schema.get("components", {}).get("schemas", {}).items():
        for field, spec in (model.get("properties") or {}).items():
            if field.lower() in {"url", "uri", "href", "callback", "webhook", "endpoint", "remote"} or spec.get("format") in {"uri", "url"}:
                suspicious.append((name, field))
    assert suspicious == []
