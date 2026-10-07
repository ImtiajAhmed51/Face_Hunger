"""Request hardening: Host and Origin checks, security headers and the optional app lock.

* **Host check** (DNS rebinding): a request is served only when its Host is an IP literal,
  ``localhost``, the configured host, or a name listed in ``LFS_ALLOWED_HOSTS``.
* **Origin check** (CSRF, second layer under the ``X-LFS-Request`` header): a state-changing
  request that carries an Origin must come from the app's own origin.
* **App lock**: optional password. When set, every ``/api`` route except the lock endpoints
  needs a session cookie (HttpOnly, SameSite=Strict). Off by default.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import secrets
import threading
import time
from typing import Optional
from urllib.parse import urlsplit

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
DEV_ORIGINS = {"127.0.0.1:5173", "localhost:5173"}
OPEN_PATHS = {"/api/lock", "/api/lock/unlock", "/api/health/live"}
COOKIE = "lfs_session"
SESSION_SECONDS = 12 * 3600
MAX_FAILURES, LOCKOUT_SECONDS = 5, 60
MIN_PASSWORD = 8
APP_CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
           "media-src 'self' blob:; font-src 'self' data:; connect-src 'self'; worker-src 'self' blob:; frame-src 'self'; "
           "object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'self'")
HEADERS = [(b"x-content-type-options", b"nosniff"), (b"referrer-policy", b"no-referrer"),
           (b"x-frame-options", b"SAMEORIGIN"), (b"cross-origin-resource-policy", b"same-origin"),
           (b"permissions-policy", b"camera=(), microphone=(), geolocation=(), payment=(), usb=()")]


def _hostname(value: str) -> str:
    value = value.strip().lower()
    if value.startswith("["):
        return value[1:value.find("]")] if "]" in value else value
    return value.rsplit(":", 1)[0] if value.count(":") == 1 else value


def host_allowed(header: str, extra: set[str]) -> bool:
    name = _hostname(header)
    if not name:
        return False
    try:
        ipaddress.ip_address(name)
        return True  # rebinding needs a DNS name; an address is what the user typed
    except ValueError:
        pass
    return name == "localhost" or name.endswith(".localhost") or name in extra


class AppLock:
    """Password check (scrypt), in-memory sessions and a global failure lockout."""

    def __init__(self, db, env_password: str = ""):
        self.db = db
        self._env = env_password or ""
        self._sessions: dict[str, float] = {}
        self._failures: list[float] = []
        self._lock = threading.Lock()

    @staticmethod
    def _derive(password: str, salt: bytes) -> bytes:
        return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)

    def _stored(self) -> Optional[dict]:
        row = self.db.one("SELECT value FROM settings WHERE key='app_lock'")
        return json.loads(row["value"]) if row else None

    @property
    def enabled(self) -> bool:
        return bool(self._env) or self._stored() is not None

    @property
    def managed_by_environment(self) -> bool:
        return bool(self._env)

    def _matches(self, password: str) -> bool:
        if self._env:
            return hmac.compare_digest(hashlib.sha256(password.encode()).digest(), hashlib.sha256(self._env.encode()).digest())
        stored = self._stored()
        if not stored:
            return False
        return hmac.compare_digest(self._derive(password, base64.b64decode(stored["salt"])), base64.b64decode(stored["hash"]))

    def retry_after(self) -> int:
        now = time.monotonic()
        with self._lock:
            self._failures = [t for t in self._failures if now - t < LOCKOUT_SECONDS]
            if len(self._failures) >= MAX_FAILURES:
                return max(1, int(LOCKOUT_SECONDS - (now - self._failures[0])))
        return 0

    def verify(self, password: str) -> bool:
        """Check a password, counting failures. Raises nothing; callers ask ``retry_after`` first."""
        ok = self._matches(password)
        with self._lock:
            if ok:
                self._failures.clear()
            else:
                self._failures.append(time.monotonic())
        return ok

    def open_session(self) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            now = time.time()
            self._sessions = {t: exp for t, exp in self._sessions.items() if exp > now}
            self._sessions[token] = now + SESSION_SECONDS
        return token

    def valid(self, token: Optional[str]) -> bool:
        if not token:
            return False
        with self._lock:
            expires = self._sessions.get(token)
            if expires is None or expires < time.time():
                self._sessions.pop(token, None)
                return False
            return True

    def close_session(self, token: Optional[str]) -> None:
        with self._lock:
            self._sessions.pop(token or "", None)

    def set_password(self, password: str) -> None:
        if self._env:
            raise ValueError("The app lock is set by LFS_APP_PASSWORD; change it there.")
        if len(password) < MIN_PASSWORD:
            raise ValueError(f"Use at least {MIN_PASSWORD} characters")
        salt = secrets.token_bytes(16)
        value = json.dumps({"salt": base64.b64encode(salt).decode(), "hash": base64.b64encode(self._derive(password, salt)).decode()})
        with self.db.connect() as conn:
            conn.execute("INSERT INTO settings(key, value) VALUES ('app_lock', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (value,))
        with self._lock:
            self._sessions.clear()

    def remove(self) -> None:
        if self._env:
            raise ValueError("The app lock is set by LFS_APP_PASSWORD; remove it there.")
        with self.db.connect() as conn:
            conn.execute("DELETE FROM settings WHERE key='app_lock'")
        with self._lock:
            self._sessions.clear()


def cookie_from(scope) -> Optional[str]:
    for key, value in scope.get("headers") or []:
        if key == b"cookie":
            for part in value.decode("latin-1").split(";"):
                name, _, token = part.strip().partition("=")
                if name == COOKIE:
                    return token
    return None


class SecurityMiddleware:
    def __init__(self, app, *, services):
        self.app = app
        self.services = services
        config = services.config
        self.hosts = {h.strip().lower() for h in str(getattr(config, "allowed_hosts", "")).replace(",", ";").split(";") if h.strip()}
        self.hosts.add(str(config.host).lower())

    async def _reject(self, send, status: int, detail: str, extra: Optional[dict] = None, headers: Optional[list] = None):
        body = json.dumps({"detail": detail, **(extra or {})}).encode()
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
                                (b"cache-control", b"no-store"), *HEADERS, *(headers or [])]})
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {k: v.decode("latin-1") for k, v in scope.get("headers") or []}
        host = headers.get(b"host", "")
        if not host_allowed(host, self.hosts):
            return await self._reject(send, 400, "This host name is not allowed. Add it to LFS_ALLOWED_HOSTS to use it.")
        method, path = scope.get("method", "GET"), scope.get("path", "")
        origin = headers.get(b"origin")
        if method not in SAFE_METHODS and origin and origin != "null":
            netloc = urlsplit(origin).netloc.lower()
            if netloc != host.lower() and netloc not in DEV_ORIGINS:
                return await self._reject(send, 403, "Cross-origin requests are not allowed")
        elif method not in SAFE_METHODS and origin == "null":
            return await self._reject(send, 403, "Cross-origin requests are not allowed")
        # CSRF: enforced here for every state-changing API call, before any body is parsed.
        if method not in SAFE_METHODS and path.startswith("/api/") and headers.get(b"x-lfs-request") != "1":
            return await self._reject(send, 403, "Missing X-LFS-Request header")
        lock = self.services.lock
        if path.startswith("/api/") and path not in OPEN_PATHS and lock.enabled and not lock.valid(cookie_from(scope)):
            return await self._reject(send, 401, "Locked. Enter the app password to continue.", {"locked": True})

        async def send_hardened(message):
            if message["type"] == "http.response.start":
                current = list(message.get("headers") or [])
                names = {k.lower() for k, _ in current}
                current += [(k, v) for k, v in HEADERS if k not in names]
                content_type = next((v for k, v in current if k.lower() == b"content-type"), b"")
                if content_type.startswith(b"text/html") and b"content-security-policy" not in names:
                    current.append((b"content-security-policy", APP_CSP.encode()))
                message = {**message, "headers": current}
            await send(message)

        await self.app(scope, receive, send_hardened)
