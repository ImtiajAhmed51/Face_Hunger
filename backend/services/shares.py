"""Connect to, list and disconnect network shares without sudo and without writing to them.

macOS first: shares are mounted the way Finder does it (AppleScript ``mount volume``), with the
script passed on standard input so the password never appears in a process list or a log. The
password is kept only for that call; "Remember" stores it in the login Keychain.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from ..netlib import ERRORS, map_mount_error, read_mounts

logger = logging.getLogger(__name__)

PROTOCOLS = {"smb": "smb", "nfs": "nfs", "afp": "afp", "webdav": "https"}
SERVER_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9._-]{0,251}[A-Za-z0-9])?$")
NAME_RE = re.compile(r"^[^\x00-\x1f\"\\<>|*?:]{1,200}$")
KEYCHAIN_LABEL = "Face Hunger"


class ShareError(Exception):
    def __init__(self, code: str, detail: Optional[str] = None):
        message, next_step = ERRORS.get(code, ERRORS["unknown"])
        super().__init__(detail or message)
        self.code, self.message, self.next_step = code, detail or message, next_step

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "next_step": self.next_step}


def _run(command: list[str], *, input_text: Optional[str] = None, timeout: float = 40.0) -> tuple[int, str]:
    try:
        done = subprocess.run(command, input=input_text, capture_output=True, text=True, timeout=timeout)
        return done.returncode, (done.stdout or "") + (done.stderr or "")
    except subprocess.TimeoutExpired:
        return 60, "timed out"
    except FileNotFoundError:
        return 127, "command not found"


def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


class ShareService:
    def __init__(self, services, *, runner: Callable = _run, mounts: Callable = read_mounts, platform: str = sys.platform):
        self.s = services
        self.run, self.mounts, self.platform = runner, mounts, platform
        self._lock = threading.Lock()

    # -- what exists ------------------------------------------------------------------
    def capabilities(self) -> dict:
        if self.platform == "darwin":
            return {"connect": bool(shutil.which("osascript")), "discover": bool(shutil.which("dns-sd")),
                    "keychain": bool(shutil.which("security")), "protocols": ["smb", "afp", "nfs", "webdav"]}
        if self.platform.startswith("linux"):
            return {"connect": bool(shutil.which("gio")), "discover": bool(shutil.which("avahi-browse")), "keychain": False,
                    "protocols": ["smb"]}
        if self.platform == "win32":
            return {"connect": True, "discover": False, "keychain": False, "protocols": ["smb"]}
        return {"connect": False, "discover": False, "keychain": False, "protocols": []}

    def list(self) -> list[dict]:
        """Every network share mounted on this machine, including ones connected in Finder."""
        libraries = self.s.db.all("SELECT id, name, path FROM libraries")
        out = []
        for mount in self.mounts():
            if not mount["network"]:
                continue
            point = mount["mount_point"]
            used = [{"id": lib["id"], "name": lib["name"]} for lib in libraries
                    if lib["path"] == point or lib["path"].startswith(point.rstrip("/") + "/")]
            out.append({"mount_point": point, "protocol": mount["protocol"], "server": mount["server"], "share": mount["share"],
                        "name": mount["share"] or os.path.basename(point), "read_only": mount["read_only"], "libraries": used})
        return out

    def find(self, server: str, share: str) -> Optional[dict]:
        for mount in self.list():
            if (mount["server"] or "").lower() == server.lower() and (mount["share"] or "").strip("/").lower() == share.strip("/").lower():
                return mount
        return None

    # -- connect ----------------------------------------------------------------------
    @staticmethod
    def validate(protocol: str, server: str, share: str, username: Optional[str]) -> None:
        if protocol not in PROTOCOLS:
            raise ShareError("unsupported", "Choose SMB, NFS, AFP or WebDAV.")
        if not SERVER_RE.match(server or ""):
            raise ShareError("unreachable", "Enter the computer's name or IP address, for example DESKTOP-PC or 192.168.1.20.")
        if not NAME_RE.match((share or "").strip("/")) or ".." in share:
            raise ShareError("share_not_found", "Enter the name of the shared folder exactly as the other computer shows it.")
        if username and not NAME_RE.match(username):
            raise ShareError("auth", "That user name contains characters that are not allowed.")

    def connect(self, *, protocol: str = "smb", server: str, share: str, username: Optional[str] = None, password: Optional[str] = None,
                guest: bool = False, remember: bool = False) -> dict:
        server, share = server.strip(), share.strip().strip("/")
        username = (username or "").strip() or None
        self.validate(protocol, server, share, username)
        existing = self.find(server, share)
        if existing:
            return {**existing, "already_connected": True}
        if not guest and username and password is None:
            password = self.recall(server, username)
        with self._lock:
            if self.platform == "darwin":
                code, output = self._connect_macos(protocol, server, share, username, password, guest)
            elif self.platform.startswith("linux"):
                code, output = self._connect_linux(protocol, server, share, username, password, guest)
            elif self.platform == "win32":
                code, output = self._connect_windows(server, share, username, password, guest)
            else:
                raise ShareError("unsupported")
        password_used, password = password, None
        if code != 0:
            error = map_mount_error(output, code)
            # The raw output can echo the address but never the password (it was only ever on stdin).
            logger.warning("Could not connect to %s share on %s: %s", protocol, server, error["code"])
            if error["code"] == "already":
                existing = self.find(server, share)
                if existing:
                    return {**existing, "already_connected": True}
            raise ShareError(error["code"])
        mount = None
        for _ in range(20):
            mount = self.find(server, share)
            if mount:
                break
            time.sleep(0.25)
        if mount is None and self.platform == "win32":
            mount = {"mount_point": f"\\\\{server}\\{share}", "protocol": "smb", "server": server.lower(), "share": share, "name": share,
                     "read_only": False, "libraries": []}
        if mount is None:
            raise ShareError("unknown", "The share was connected but could not be found afterwards. Look for it under Network shares.")
        if remember and username and password_used and not guest:
            self.remember(server, username, password_used)
        return {**mount, "already_connected": False}

    def _connect_macos(self, protocol, server, share, username, password, guest):
        scheme = PROTOCOLS[protocol]
        if protocol == "nfs":
            url = f"nfs://{server}/{share}"
        elif guest:
            url = f"{scheme}://guest@{server}/{share.replace(' ', '%20')}"
        else:
            url = f"{scheme}://{server}/{share.replace(' ', '%20')}"
        script = f"mount volume {_quote(url)}"
        if not guest and username and protocol != "nfs":
            script += f" as user name {_quote(username)}"
            if password:
                script += f" with password {_quote(password)}"
        # "-" makes osascript read the script from stdin: the password is never an argument.
        return self.run(["/usr/bin/osascript", "-"], input_text=f"with timeout of 30 seconds\n{script}\nend timeout\n", timeout=45)

    def _connect_linux(self, protocol, server, share, username, password, guest):
        if protocol != "smb" or not shutil.which("gio"):
            raise ShareError("unsupported", f"Connect it yourself, then pick it under Network shares: "
                                            f"sudo mount -t cifs //{server}/{share} /mnt/{share} -o ro,username=USER")
        answers = "" if guest else f"{username or ''}\n\n{password or ''}\n"
        return self.run(["gio", "mount", "-a" if guest else "--", f"smb://{server}/{share}"] if guest
                        else ["gio", "mount", f"smb://{server}/{share}"], input_text=answers, timeout=45)

    def _connect_windows(self, server, share, username, password, guest):
        unc = f"\\\\{server}\\{share}"
        if guest or not username:
            return (0, "") if os.path.isdir(unc) else self.run(["net", "use", unc], timeout=45)
        # "*" makes net use read the password from the console (stdin), not from the command line.
        return self.run(["net", "use", unc, "*", f"/user:{username}", "/persistent:no"], input_text=f"{password or ''}\n", timeout=45)

    # -- disconnect -------------------------------------------------------------------
    def disconnect(self, mount_point: str, *, force: bool = False) -> dict:
        mount = next((m for m in self.list() if m["mount_point"] == mount_point), None)
        if mount is None:
            raise ShareError("share_not_found", "That share is not connected.")
        busy = self.s.worker.latest() if self.s.worker.busy else None
        if busy and any(lib["id"] == busy.get("library_id") for lib in mount["libraries"]) and not force:
            raise ShareError("already", "A scan is reading this share right now. Stop the scan first, or confirm to disconnect anyway.")
        if self.platform == "darwin":
            code, output = self.run(["/usr/sbin/diskutil", "unmount", mount_point], timeout=30)
        elif self.platform == "win32":
            code, output = self.run(["net", "use", mount_point, "/delete", "/y"], timeout=30)
        else:
            code, output = self.run(["gio", "mount", "-u", mount_point], timeout=30) if shutil.which("gio") else self.run(["umount", mount_point], timeout=30)
        if code != 0:
            raise ShareError("unknown", "The share could not be disconnected: a file on it is still open. Close it and try again.")
        return {"ok": True, "mount_point": mount_point}

    # -- discovery ---------------------------------------------------------------------
    def discover(self, seconds: float = 2.5) -> list[dict]:
        """Computers announcing file sharing on the local network (Bonjour). Empty when the tool is missing."""
        if self.platform == "darwin" and shutil.which("dns-sd"):
            try:
                proc = subprocess.Popen(["dns-sd", "-B", "_smb._tcp", "local."], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
            except OSError:
                return []
            timer = threading.Timer(seconds, proc.terminate)
            timer.start()
            try:
                output = proc.communicate(timeout=seconds + 3)[0]
            except subprocess.TimeoutExpired:
                proc.kill()
                output = ""
            finally:
                timer.cancel()
            return self.parse_dns_sd(output)
        if self.platform.startswith("linux") and shutil.which("avahi-browse"):
            code, output = self.run(["avahi-browse", "-t", "-p", "_smb._tcp"], timeout=seconds + 3)
            names = {line.split(";")[3] for line in output.splitlines() if line.startswith("+") and len(line.split(";")) > 3}
            return [{"name": n, "server": f"{n}.local", "protocol": "smb"} for n in sorted(names)]
        return []

    @staticmethod
    def parse_dns_sd(output: str) -> list[dict]:
        found = {}
        for line in output.splitlines():
            match = re.match(r"^\s*\d+:\d+:\d+\.\d+\s+Add\s+\d+\s+\d+\s+(\S+)\s+_smb\._tcp\.\s+(.+?)\s*$", line)
            if match:
                name = match.group(2)
                host = re.sub(r"[^A-Za-z0-9-]+", "-", name).strip("-")
                found[name] = {"name": name, "server": f"{host}.local", "protocol": "smb"}
        return sorted(found.values(), key=lambda item: item["name"].lower())

    # -- keychain ------------------------------------------------------------------------
    def remember(self, server: str, username: str, password: str) -> bool:
        if self.platform != "darwin" or not shutil.which("security"):
            return False
        # "security -i" reads the command from stdin, so the password is not visible to other processes.
        command = (f"add-internet-password -U -a {_quote(username)} -s {_quote(server)} -r \"smb \" "
                   f"-l {_quote(KEYCHAIN_LABEL + ': ' + server)} -w {_quote(password)}\n")
        code, _ = self.run(["/usr/bin/security", "-i"], input_text=command, timeout=15)
        return code == 0

    def recall(self, server: str, username: str) -> Optional[str]:
        if self.platform != "darwin" or not shutil.which("security"):
            return None
        code, output = self.run(["/usr/bin/security", "find-internet-password", "-a", username, "-s", server, "-r", "smb ", "-w"], timeout=15)
        return output.strip() if code == 0 and output.strip() else None

    def forget(self, server: str, username: str) -> bool:
        if self.platform != "darwin" or not shutil.which("security"):
            return False
        return self.run(["/usr/bin/security", "delete-internet-password", "-a", username, "-s", server, "-r", "smb "], timeout=15)[0] == 0


def managed_mount_dir() -> Path:
    return Path.home() / "FaceHuntMounts"
