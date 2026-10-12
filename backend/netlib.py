"""Helpers for libraries on network shares and other removable roots.

Pure functions (path identity, junk filters, mount parsing, error mapping) plus two small
I/O utilities (a reachability probe that cannot hang, and a retry wrapper for transient
network errors). Nothing here ever writes to a library folder.
"""

from __future__ import annotations

import errno
import os
import re
import sys
import threading
import time
import unicodedata
from pathlib import Path, PureWindowsPath
from typing import Callable, Optional

# Folders and files that are never content, whatever the share came from.
JUNK_DIRS = frozenset(n.casefold() for n in (
    "$RECYCLE.BIN", "System Volume Information", ".Trashes", ".Trash", ".Spotlight-V100", ".fseventsd", "@eaDir",
    "#recycle", "#snapshot", "lost+found", ".TemporaryItems", ".DocumentRevisions-V100", "RECYCLER", "$Recycle.Bin",
    ".AppleDouble", ".AppleDB", ".AppleDesktop", "Network Trash Folder", "Temporary Items", "@Recycle", "@Recently-Snapshot",
))
JUNK_FILES = frozenset(n.casefold() for n in (".DS_Store", "Thumbs.db", "desktop.ini", "ehthumbs.db", ".localized", "Icon\r"))
NETWORK_FS = {"smbfs": "smb", "cifs": "smb", "smb3": "smb", "nfs": "nfs", "nfs4": "nfs", "afpfs": "afp", "webdav": "webdav",
              "davfs": "webdav", "fuse.sshfs": "sshfs", "fuse.gvfsd-fuse": "gvfs", "9p": "9p"}
TRANSIENT = frozenset(filter(None, (getattr(errno, name, None) for name in (
    "ETIMEDOUT", "ESTALE", "EAGAIN", "EIO", "EINTR", "EBUSY", "ENETDOWN", "ENETUNREACH", "ENETRESET", "ECONNRESET",
    "ECONNABORTED", "EHOSTDOWN", "EHOSTUNREACH", "ENOTCONN", "EPIPE", "ENOLINK"))))


class LibraryOffline(RuntimeError):
    """The library's root cannot be reached right now. Never a reason to mark anything missing."""


def is_junk_dir(name: str) -> bool:
    return name.startswith(".") or name.casefold() in JUNK_DIRS


def is_junk_file(name: str) -> bool:
    return name.startswith("._") or name.casefold() in JUNK_FILES or name.endswith((".part", ".crdownload", ".tmp", ".partial"))


# -- path identity ---------------------------------------------------------------------------------
def split_any(path: str) -> list[str]:
    """Path components for POSIX, Windows drive and UNC spellings alike."""
    text = str(path)
    if text.startswith("\\\\?\\UNC\\"):
        text = "\\\\" + text[8:]
    elif text.startswith("\\\\?\\"):
        text = text[4:]
    return [p for p in re.split(r"[\\/]+", text) if p]


def relative_identity(root: str, path: str) -> tuple[str, str]:
    """``(rel_path, rel_key)`` of ``path`` under ``root``.

    ``rel_path`` is how the file is addressed inside the library: forward slashes, Unicode NFC.
    ``rel_key`` is what makes two spellings the same file: NFC, case-folded, and with the trailing
    dots and spaces Windows ignores removed from every component. A share opened from macOS (NFD
    names), Windows (backslashes, any case) or Linux therefore maps to one row per file.
    """
    root_parts, parts = split_any(root), split_any(path)
    fold = [unicodedata.normalize("NFC", p).casefold() for p in root_parts]
    head = [unicodedata.normalize("NFC", p).casefold() for p in parts[:len(root_parts)]]
    if head != fold:
        raise ValueError(f"{path} is not inside {root}")
    rest = [unicodedata.normalize("NFC", p) for p in parts[len(root_parts):]]
    rel_path = "/".join(rest)
    rel_key = "/".join((p.rstrip(". ") or p).casefold() for p in rest)
    return rel_path, rel_key


def join_root(root: str, rel_path: str) -> str:
    """Absolute path of ``rel_path`` under ``root`` in the spelling of the machine running the app."""
    if re.match(r"^(\\\\|[A-Za-z]:)", str(root)):
        return str(PureWindowsPath(root).joinpath(*rel_path.split("/")))
    return str(Path(root).joinpath(*rel_path.split("/"))) if rel_path else str(root)


# -- mounts ---------------------------------------------------------------------------------------
def parse_share_source(source: str) -> dict:
    """``//user@server/share``, ``server:/export`` or ``\\\\server\\share`` -> identity (never the password)."""
    text = source.replace("\\", "/")
    match = re.match(r"^//(?:[^@/]*@)?([^/]+)/(.+)$", text)
    if match:
        return {"server": match.group(1).split(":")[0].lower(), "share": match.group(2).strip("/").replace("%20", " ")}
    match = re.match(r"^([^:/]+):(/.*)$", text)
    if match:
        return {"server": match.group(1).lower(), "share": match.group(2)}
    match = re.match(r"^https?://([^/]+)/?(.*)$", text)
    if match:
        return {"server": match.group(1).lower(), "share": match.group(2)}
    return {"server": None, "share": None}


def parse_mounts(text: str, platform: str = sys.platform) -> list[dict]:
    """Mount table from ``mount`` (macOS/BSD) or ``/proc/mounts`` (Linux)."""
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if platform == "darwin":
            match = re.match(r"^(.*?) on (.*) \(([^,)]+)(.*)\)$", line)
            if not match:
                continue
            source, point, fstype, flags = match.group(1), match.group(2), match.group(3).strip(), match.group(4)
        else:
            fields = line.split(" ")
            if len(fields) < 4:
                continue
            source, point, fstype, flags = fields[0], fields[1].replace("\\040", " "), fields[2], fields[3]
        protocol = NETWORK_FS.get(fstype)
        out.append({"source": re.sub(r"//[^@/]*@", "//", source), "mount_point": point, "fstype": fstype,
                    "network": protocol is not None, "protocol": protocol, "read_only": bool(re.search(r"\bread-only\b|\bro\b", flags)),
                    **(parse_share_source(source) if protocol else {"server": None, "share": None})})
    return out


def read_mounts() -> list[dict]:
    try:
        if sys.platform == "darwin":
            import subprocess

            return parse_mounts(subprocess.run(["/sbin/mount"], capture_output=True, text=True, timeout=5).stdout, "darwin")
        if sys.platform.startswith("linux"):
            return parse_mounts(Path("/proc/mounts").read_text(), "linux")
    except Exception:
        pass
    return []


def classify(path: str, mounts: Optional[list[dict]] = None) -> dict:
    """Where a folder lives: ``local``, ``external`` (another volume) or ``network`` (with the share identity)."""
    text = str(path)
    if text.startswith("\\\\") or text.startswith("//"):
        return {"kind": "network", "protocol": "smb", **parse_share_source(text), "mount_point": None}
    if sys.platform == "win32" and re.match(r"^[A-Za-z]:", text):
        try:
            import ctypes

            kind = ctypes.windll.kernel32.GetDriveTypeW(text[:2] + "\\")  # 4 = remote, 2 = removable
            return {"kind": "network" if kind == 4 else "external" if kind == 2 else "local", "protocol": "smb" if kind == 4 else None,
                    "server": None, "share": None, "mount_point": text[:2] + "\\"}
        except Exception:
            return {"kind": "local", "protocol": None, "server": None, "share": None, "mount_point": None}
    mounts = read_mounts() if mounts is None else mounts
    best = None
    for mount in mounts:
        point = mount["mount_point"].rstrip("/") or "/"
        if text == point or text.startswith(point + "/") or point == "/":
            if best is None or len(point) > len(best["mount_point"].rstrip("/") or "/"):
                best = mount
    if best is None:
        return {"kind": "local", "protocol": None, "server": None, "share": None, "mount_point": None}
    point = best["mount_point"]
    if best["network"]:
        kind = "network"
    elif point.startswith(("/Volumes/", "/media/", "/run/media/", "/mnt/")):
        kind = "external"
    else:
        kind = "local"
    return {"kind": kind, "protocol": best["protocol"], "server": best["server"], "share": best["share"], "mount_point": point}


# -- reachability and retries ---------------------------------------------------------------------
def run_with_timeout(fn: Callable, timeout: float, default=None):
    """Run ``fn`` in a helper thread and give up waiting after ``timeout`` (a dead SMB mount can block forever)."""
    box: dict = {}

    def work():
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001
            box["error"] = exc

    thread = threading.Thread(target=work, daemon=True, name="fh-io-probe")
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        return default, TimeoutError(f"no answer within {timeout:g}s")
    return box.get("value", default), box.get("error")


def probe(path: str, timeout: float = 3.0) -> tuple[bool, Optional[str]]:
    """Is this folder there and listable right now? Returns ``(online, reason)``; never hangs."""
    def look():
        with os.scandir(path) as entries:
            next(entries, None)
        return True

    ok, error = run_with_timeout(look, timeout, False)
    if isinstance(error, TimeoutError):
        # A sleeping share (spun-down disk, idle SMB session) can take several seconds to answer the
        # first request. One longer second look avoids calling it offline when it is only waking up.
        ok, error = run_with_timeout(look, max(timeout * 3, 8.0), False)
    if error is None and ok:
        return True, None
    return False, "slow or unreachable" if isinstance(error, TimeoutError) else type(error).__name__ if error else "unreachable"


def is_transient(exc: BaseException) -> bool:
    return isinstance(exc, TimeoutError) or (isinstance(exc, OSError) and exc.errno in TRANSIENT)


def retry_io(fn: Callable, *, attempts: int = 4, base_delay: float = 0.25, sleep: Callable[[float], None] = time.sleep,
             on_retry: Optional[Callable[[int, BaseException], None]] = None):
    """Call ``fn``; on a transient network error wait 0.25 s, 0.5 s, 1 s ... and try again."""
    for attempt in range(attempts):
        try:
            return fn()
        except Exception as exc:
            if not is_transient(exc) or attempt == attempts - 1:
                raise
            if on_retry:
                on_retry(attempt + 1, exc)
            sleep(base_delay * (2 ** attempt))


def stable_size(path: str, wait: float = 0.0, sleep: Callable[[float], None] = time.sleep) -> Optional[int]:
    """Size of a finished file, or None for an empty file or one still being copied (size changed between two looks)."""
    first = os.stat(path)
    if first.st_size == 0:
        return None
    if wait > 0:
        sleep(wait)
        second = os.stat(path)
        if (second.st_size, second.st_mtime_ns) != (first.st_size, first.st_mtime_ns):
            return None
    return first.st_size


# -- mount failures -> something a person can act on ---------------------------------------------
ERRORS = {
    "auth": ("The user name or password was not accepted.",
             "Check the account on the other computer. On Windows the account needs a password and must be allowed on the shared folder."),
    "unreachable": ("That computer could not be reached.",
                    "Check that it is switched on, awake and on the same network, and that the name or IP address is right."),
    "firewall": ("The computer answered but refused the file-sharing connection.",
                 "On Windows: turn on \"File and printer sharing\" (Settings > Network > Advanced sharing settings) and allow it through the firewall."),
    "share_not_found": ("That computer has no shared folder with this name.",
                        "Check the share name. On Windows: right-click the folder > Properties > Sharing shows the exact name."),
    "permission": ("This account is not allowed to open that shared folder.",
                   "On the other computer, give this account Read permission on the share and on the folder."),
    "protocol": ("The two computers could not agree on a file-sharing version.",
                 "Very old devices only speak SMB1, which modern systems refuse. Enable SMB2 or newer on the device."),
    "already": ("This share is already connected.", "Pick it under Network shares."),
    "unsupported": ("This computer cannot connect to that kind of share by itself.", "Connect it with your system's file manager, then pick it under Network shares."),
    "timeout": ("The share is slow or unreachable.", "Retry. If it keeps happening, check the network connection."),
    "unknown": ("The share could not be connected.", "Try connecting in Finder (Go > Connect to Server) to see the system's own message."),
}
_PATTERNS = (
    ("auth", r"authentication (error|failed)|logon failure|bad (user ?name|password)|access denied.*password|error 1326|error 86\b|-5045|-5002|EAUTH|NT_STATUS_LOGON_FAILURE|-128\b.*password|user name or password"),
    ("share_not_found", r"no such file or directory|bad network (name|path)|error 67\b|NT_STATUS_BAD_NETWORK_NAME|share (does not|doesn't) exist|-36\b.*share|ENOENT|can't be found|could not be found"),
    ("permission", r"permission denied|operation not permitted|NT_STATUS_ACCESS_DENIED|error 5\b|EACCES|-5000\b"),
    ("firewall", r"connection refused|ECONNREFUSED|error 1231|actively refused"),
    ("protocol", r"protocol (negotiation|not supported)|NT_STATUS_CONNECTION_RESET|operation not supported|SMB1|dialect|EPROTO|ENOTSUP"),
    ("timeout", r"timed out|timeout|ETIMEDOUT|-5016"),
    ("unreachable", r"no route to host|host is down|could not (resolve|connect)|unknown host|network is unreachable|server.*(not exist|unavailable|can.t be found)|error 53\b|EHOSTUNREACH|EHOSTDOWN|ENETUNREACH|-5014|-1728|nodename nor servname|name or service not known"),
    ("already", r"file exists|already (mounted|connected)|EEXIST|error 85\b|error 1219"),
)


def map_mount_error(output: str, returncode: Optional[int] = None) -> dict:
    text = output or ""
    code = "unknown"
    for name, pattern in _PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            code = name
            break
    else:
        code = {13: "permission", 1: "unknown", 2: "share_not_found", 60: "timeout", 61: "firewall", 64: "unreachable", 65: "unreachable",
                77: "auth", 80: "auth", 68: "unreachable"}.get(returncode or 0, "unknown")
    message, next_step = ERRORS[code]
    return {"code": code, "message": message, "next_step": next_step}


# -- read-only guarantee for network libraries ---------------------------------------------------
class ReadOnlyLibrary(PermissionError):
    """Something tried to write inside a network library. Face Hunger only ever reads from shares."""


_protected: tuple[str, ...] = ()
_guard_installed = False
_WRITE_EVENTS = frozenset({"os.remove", "os.unlink", "os.rename", "os.replace", "os.mkdir", "os.rmdir", "os.chmod", "os.chown",
                           "os.truncate", "os.symlink", "os.link", "os.utime", "os.chflags", "os.setxattr", "os.removexattr",
                           "shutil.move", "shutil.copyfile", "shutil.copymode", "shutil.copystat", "shutil.rmtree"})


def protect(roots) -> None:
    """Folders (network library roots) that this process must never modify."""
    global _protected
    wanted = set()
    for root in roots:
        text = str(root).rstrip("/\\")
        if text:
            wanted.add(text)
            try:
                wanted.add(os.path.realpath(text))
            except OSError:
                pass
    _protected = tuple(sorted(wanted))
    if _protected:
        install_write_guard()


def is_protected(path) -> bool:
    if not _protected:
        return False
    try:
        text = os.fspath(path)
    except TypeError:
        return False
    if isinstance(text, bytes):
        text = os.fsdecode(text)
    return any(text == root or text.startswith(root + "/") or text.startswith(root + "\\") for root in _protected)


_deletable = threading.local()


class allow_delete:
    """The one exception to read-only: the user confirmed a permanent delete of exactly this file.

    Inside the block, this thread may remove that single path on a network library. Writing, renaming
    and every other path stay refused.
    """

    def __init__(self, path):
        self.path = os.fspath(path)

    def __enter__(self):
        paths = getattr(_deletable, "paths", None)
        if paths is None:
            paths = _deletable.paths = set()
        paths.add(self.path)
        return self

    def __exit__(self, *exc):
        _deletable.paths.discard(self.path)
        return False


def _write_guard(event: str, args) -> None:
    if not _protected:
        return
    if event in ("os.remove", "os.unlink") and args and isinstance(args[0], (str, os.PathLike)) \
            and os.fspath(args[0]) in getattr(_deletable, "paths", ()):
        return
    if event == "open":
        path, mode, flags = (tuple(args) + (None, None, None))[:3]
        if isinstance(path, int) or path is None:
            return
        writing = (isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)) \
            or (isinstance(mode, str) and any(c in mode for c in "wax+"))
        if writing and is_protected(path):
            raise ReadOnlyLibrary(f"Face Hunger does not write to network libraries: {os.fspath(path)}")
    elif event in _WRITE_EVENTS:
        for value in args[:2]:
            if isinstance(value, (str, bytes, os.PathLike)) and is_protected(value):
                raise ReadOnlyLibrary(f"Face Hunger does not change files on network libraries: {os.fspath(value)}")


def install_write_guard() -> None:
    """Refuse, at the interpreter level, every write, rename or delete inside a protected folder.

    An audit hook sees file operations from all Python code in the process (this app, its libraries),
    so the read-only promise does not depend on each feature remembering to check.
    """
    global _guard_installed
    if not _guard_installed:
        sys.addaudithook(_write_guard)
        _guard_installed = True
