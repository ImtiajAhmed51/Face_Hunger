"""Child process that runs one plugin. Standard library only; started by ``host.py``.

    python -I -B runner.py '<json config>'

The parent talks JSON lines over stdin/stdout. Before any plugin code is imported an
audit hook is installed (audit hooks cannot be removed) that refuses:

* file reads outside Python itself, the plugin folder and granted roots,
* every file write/delete outside the plugin's own storage folder (when granted),
* all sockets and name lookups unless ``network`` is granted,
* starting processes and loading native libraries through ctypes, always.

On macOS the parent additionally wraps this process in a kernel sandbox profile, so
the same rules hold for native code. See docs/EXTENDING.md for the limits.
"""

import base64
import importlib
import json
import os
import sys
import threading

MAX_LINE = 64 * 1024 * 1024

WRITE_EVENTS = {"os.remove", "os.unlink", "os.rename", "os.replace", "os.mkdir", "os.rmdir", "os.chmod", "os.chown",
                "os.truncate", "os.symlink", "os.link", "os.utime", "os.chflags", "os.setxattr", "os.removexattr",
                "os.mkfifo", "os.mknod", "shutil.copyfile", "shutil.copymode", "shutil.copystat", "shutil.move",
                "shutil.rmtree", "shutil.chown", "shutil.make_archive", "shutil.unpack_archive", "sqlite3.connect",
                "tempfile.mkstemp", "tempfile.mkdtemp"}
READ_EVENTS = {"os.listdir", "os.scandir", "os.walk", "os.fwalk", "os.getxattr", "os.listxattr", "glob.glob", "os.chdir"}
PROCESS_EVENTS = ("subprocess.Popen", "os.system", "os.exec", "os.fork", "os.forkpty", "os.posix_spawn", "os.spawn",
                  "os.startfile", "os.kill", "os.killpg", "pty.spawn", "ctypes.dlsym", "os.setuid", "os.setgid")
NETWORK_PREFIXES = ("socket.", "urllib.", "http.client.", "ftplib.", "smtplib.", "imaplib.", "poplib.", "nntplib.",
                    "telnetlib.", "ssl.", "webbrowser.")


class Denied(PermissionError):
    pass


def install_sandbox(config: dict) -> None:
    real = os.path.realpath
    permissions = set(config.get("permissions") or [])
    read_roots = {real(p) for p in {sys.prefix, sys.base_prefix, sys.exec_prefix, *filter(None, sys.path),
                                    config["plugin_dir"], "/usr/share/zoneinfo"} if p}
    write_roots = set()
    if "storage" in permissions and config.get("storage_dir"):
        write_roots.add(real(config["storage_dir"]))
        read_roots.add(real(config["storage_dir"]))
    if "files.read" in permissions:
        read_roots.update(real(p) for p in config.get("read_roots") or [])
    network = "network" in permissions
    harmless = {"/dev/null", "/dev/urandom", "/dev/random"}
    busy = threading.local()

    def inside(path, roots) -> bool:
        if isinstance(path, int):  # an already-open descriptor
            return True
        if path is None:
            path = "."
        try:
            path = real(os.fsdecode(path))
        except Exception:
            return False
        return path in harmless or any(path == root or path.startswith(root + os.sep) for root in roots)

    def need(path, roots, what):
        if not inside(path, roots):
            raise Denied(f"plugin sandbox: {what} is not permitted: {path!r}")

    def hook(event: str, args):
        if getattr(busy, "on", False):
            return
        busy.on = True
        try:
            if event == "open":
                path, mode, flags = (list(args) + [None, None, None])[:3]
                writing = bool(isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)) \
                    or (isinstance(mode, str) and any(c in mode for c in "wax+"))
                need(path, write_roots if writing else read_roots, "writing" if writing else "reading")
            elif event in WRITE_EVENTS:
                for value in args[:2]:
                    if isinstance(value, (str, bytes, os.PathLike)):
                        need(value, write_roots, "writing")
            elif event in READ_EVENTS:
                need(args[0] if args else None, read_roots, "reading")
            elif event == "ctypes.dlopen":
                if args and args[0] is not None:
                    raise Denied("plugin sandbox: loading native libraries is not permitted")
            elif event.startswith(PROCESS_EVENTS):
                raise Denied(f"plugin sandbox: {event} is not permitted")
            elif not network and event.startswith(NETWORK_PREFIXES):
                raise Denied("plugin sandbox: network access is not permitted (grant the 'network' permission)")
        finally:
            busy.on = False

    sys.addaudithook(hook)


class Plugin:
    """Resolves ``module:attr`` entries lazily and dispatches ``capability.method`` calls."""

    def __init__(self, config: dict):
        self.config = config
        self._objects: dict = {}

    def target(self, capability: str, method: str):
        if capability not in self._objects:
            entry = self.config["entries"].get(capability)
            if not entry:
                raise LookupError(f"This plugin does not provide '{capability}'")
            module_name, _, attr = entry.partition(":")
            obj = getattr(importlib.import_module(module_name), attr)
            self._objects[capability] = obj() if isinstance(obj, type) else obj
        obj = self._objects[capability]
        fn = getattr(obj, method, None)
        if fn is None and callable(obj):
            fn = obj
        if fn is None:
            raise LookupError(f"'{capability}' entry has no '{method}'")
        return fn

    def call(self, method: str, params: dict):
        if method == "ping":
            return {"pong": True, "pid": os.getpid()}
        capability, _, name = method.partition(".")
        if "images_b64" in params:
            params = dict(params)
            params["images"] = [base64.b64decode(b) for b in params.pop("images_b64")]
        return self.target(capability, name)(**params)


def main() -> int:
    config = json.loads(sys.argv[1])
    # The reply channel is private; anything the plugin prints goes to the log (stderr).
    channel = os.fdopen(os.dup(1), "wb")
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    stdin = sys.stdin.buffer
    sys.path.insert(0, config["plugin_dir"])
    install_sandbox(config)
    plugin = Plugin(config)
    while True:
        line = stdin.readline(MAX_LINE + 1)
        if not line:
            return 0
        request_id = None
        try:
            request = json.loads(line)
            request_id = request.get("id")
            reply = {"id": request_id, "ok": True, "result": plugin.call(request["method"], request.get("params") or {})}
            payload = json.dumps(reply)
        except BaseException as exc:  # noqa: BLE001 - a plugin error must become a reply, not a dead channel
            if isinstance(exc, (SystemExit, KeyboardInterrupt)):
                raise
            payload = json.dumps({"id": request_id, "ok": False, "denied": isinstance(exc, PermissionError),
                                  "error": f"{type(exc).__name__}: {exc}"[:2000]})
        channel.write(payload.encode() + b"\n")
        channel.flush()


if __name__ == "__main__":
    sys.exit(main())
