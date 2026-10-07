"""Parent side of a plugin: one subprocess per plugin, JSON-lines RPC with timeouts.

A plugin that crashes, hangs or floods its output only loses its own process: the call
raises ``PluginError`` and the next call starts a fresh process.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

RUNNER = Path(__file__).with_name("runner.py")
MAX_LINE = 64 * 1024 * 1024
SANDBOX_EXEC = "/usr/bin/sandbox-exec"
_kernel_sandbox: Optional[bool] = None


class PluginError(RuntimeError):
    def __init__(self, message: str, *, denied: bool = False, crashed: bool = False):
        super().__init__(message)
        self.denied = denied
        self.crashed = crashed


def kernel_sandbox_available() -> bool:
    """macOS only: can this process start children under a sandbox profile?"""
    global _kernel_sandbox
    if _kernel_sandbox is None:
        ok = False
        if sys.platform == "darwin" and os.path.exists(SANDBOX_EXEC) and shutil.which("true"):
            try:
                ok = subprocess.run([SANDBOX_EXEC, "-p", "(version 1)(allow default)", shutil.which("true")],
                                    capture_output=True, timeout=10).returncode == 0
            except Exception:
                ok = False
        _kernel_sandbox = ok
    return _kernel_sandbox


def _quoted(path) -> str:
    return '"' + os.path.realpath(str(path)).replace("\\", "\\\\").replace('"', '\\"') + '"'


def sandbox_profile(config: dict, protected: list) -> str:
    """Deny-rules for the kernel sandbox. Later rules win, so allows follow the denies."""
    permissions = set(config.get("permissions") or [])
    lines = ["(version 1)", "(allow default)"]
    if "network" not in permissions:
        lines.append("(deny network*)")
    lines.append('(deny file-write* (subpath "/"))')
    lines.append('(allow file-write* (literal "/dev/null") (literal "/dev/dtracehelper"))')
    roots = list(protected) + ([] if "files.read" in permissions else list(config.get("read_roots") or []))
    for root in roots:
        lines.append(f"(deny file-read* (subpath {_quoted(root)}))")
    lines.append(f"(allow file-read* (subpath {_quoted(config['plugin_dir'])}))")
    if "storage" in permissions and config.get("storage_dir"):
        lines.append(f"(allow file-read* file-write* (subpath {_quoted(config['storage_dir'])}))")
    return "\n".join(lines)


class PluginProcess:
    def __init__(self, plugin_id: str, config: dict, *, log_path: Path, protected: Optional[list] = None):
        self.id = plugin_id
        self.config = config
        self.log_path = Path(log_path)
        self.protected = protected or []
        self._lock = threading.Lock()
        self._proc: Optional[subprocess.Popen] = None
        self._replies: "queue.Queue" = queue.Queue()
        self._next_id = 0
        self.starts = 0
        self.crashes = 0
        self.last_error: Optional[str] = None
        self.kernel_sandbox = False

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    @property
    def pid(self) -> Optional[int]:
        return self._proc.pid if self.running else None

    def _start(self) -> None:
        command = [sys.executable, "-I", "-B", str(RUNNER), json.dumps(self.config)]
        self.kernel_sandbox = kernel_sandbox_available()
        if self.kernel_sandbox:
            command = [SANDBOX_EXEC, "-p", sandbox_profile(self.config, self.protected), *command]
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        log = open(self.log_path, "ab")
        try:
            if log.tell() > 2 * 1024 * 1024:
                log.truncate(0)
            # A minimal environment: nothing from the server's own environment leaks in.
            env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "PYTHONIOENCODING": "utf-8"}
            self._proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log, env=env,
                                          cwd=self.config["plugin_dir"], start_new_session=True)
        finally:
            log.close()
        self._replies = queue.Queue()
        self.starts += 1
        threading.Thread(target=self._read, args=(self._proc, self._replies), daemon=True, name=f"plugin-{self.id}").start()

    @staticmethod
    def _read(proc: subprocess.Popen, replies: "queue.Queue") -> None:
        try:
            while True:
                line = proc.stdout.readline(MAX_LINE + 1)
                if not line or len(line) > MAX_LINE:
                    break
                replies.put(line)
        except Exception:
            pass
        replies.put(None)

    def _kill(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            proc.kill()
            proc.wait(timeout=5)
        except Exception:
            pass
        for stream in (proc.stdin, proc.stdout):
            try:
                stream.close()
            except Exception:
                pass

    def _fail(self, message: str) -> PluginError:
        self.crashes += 1
        self.last_error = message
        self._kill()
        logger.warning("Plugin %s: %s", self.id, message)
        return PluginError(message, crashed=True)

    def call(self, method: str, params: Optional[dict] = None, *, timeout: float = 30.0):
        with self._lock:
            if not self.running:
                self._kill()
                self._start()
            self._next_id += 1
            request_id = self._next_id
            try:
                self._proc.stdin.write(json.dumps({"id": request_id, "method": method, "params": params or {}}).encode() + b"\n")
                self._proc.stdin.flush()
            except (BrokenPipeError, OSError):
                raise self._fail(f"The plugin process stopped before '{method}' could be sent") from None
            try:
                line = self._replies.get(timeout=timeout)
            except queue.Empty:
                raise self._fail(f"The plugin did not answer '{method}' within {timeout:g}s and was stopped") from None
            if line is None:
                code = self._proc.poll() if self._proc else None
                raise self._fail(f"The plugin process ended unexpectedly during '{method}' (exit code {code})")
            try:
                reply = json.loads(line)
                if reply.get("id") != request_id:
                    raise ValueError("unexpected reply id")
            except ValueError:
                raise self._fail(f"The plugin sent an invalid reply to '{method}'") from None
            if not reply.get("ok"):
                self.last_error = reply.get("error")
                raise PluginError(str(reply.get("error")), denied=bool(reply.get("denied")))
            return reply.get("result")

    def stop(self) -> None:
        with self._lock:
            self._kill()

    def status(self) -> dict:
        return {"running": self.running, "pid": self.pid, "starts": self.starts, "crashes": self.crashes,
                "last_error": self.last_error, "kernel_sandbox": self.kernel_sandbox}
