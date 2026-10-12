"""Folder browser for the chooser: places to start from, sub-folders, the system's own picker.

These routes reveal folder names on the machine running the server, so they answer only to the
person at that machine (a loopback client) or to a signed-in session when the app lock is on.
They never create, change or delete anything.
"""

from __future__ import annotations

import ipaddress
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Request

from ..deps import config, services
from ..netlib import classify, is_junk_dir, is_junk_file, read_mounts, run_with_timeout
from ..scanner import IMAGE, VIDEO
from ..schemas import FsAllowBody
from ..services.presenters import _require_csrf

router = APIRouter()
LIST_LIMIT = 2000
COUNT_SECONDS = 3.0


def is_local_client(request: Request) -> bool:
    host = request.client.host if request.client else ""
    if host == "testclient":  # Starlette's in-process test client
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def fs_guard(request: Request) -> None:
    _require_csrf(request)
    if not is_local_client(request) and not services().lock.enabled:
        raise HTTPException(403, "Browsing folders from another device needs the app lock. Turn it on in Settings, or use this on the computer running Face Hunger.")


def native_command() -> Optional[list[str]]:
    if sys.platform == "darwin" and shutil.which("osascript"):
        return ["/usr/bin/osascript", "-e", 'POSIX path of (choose folder with prompt "Choose a folder for Face Hunger")']
    if sys.platform == "win32" and shutil.which("powershell"):
        return ["powershell", "-NoProfile", "-Command",
                "Add-Type -AssemblyName System.Windows.Forms; $d = New-Object System.Windows.Forms.FolderBrowserDialog; "
                "if ($d.ShowDialog() -eq 'OK') { $d.SelectedPath }"]
    if sys.platform.startswith("linux") and os.environ.get("DISPLAY"):
        if shutil.which("zenity"):
            return ["zenity", "--file-selection", "--directory", "--title=Choose a folder for Face Hunger"]
        if shutil.which("kdialog"):
            return ["kdialog", "--getexistingdirectory", str(Path.home())]
    return None


def _place(path: Path, name: str, group: str, mounts: list[dict]) -> Optional[dict]:
    if not path.is_dir():
        return None
    info = classify(str(path), mounts)
    return {"name": name, "path": str(path), "group": group, "kind": info["kind"], "is_network": info["kind"] == "network",
            "server": info.get("server"), "share": info.get("share")}


@router.get("/api/fs/roots")
def fs_roots(request: Request):
    fs_guard(request)
    mounts = read_mounts()
    home = Path.home()
    places = []
    if sys.platform == "win32":
        import string

        for letter in string.ascii_uppercase:
            drive = Path(f"{letter}:\\")
            if drive.exists():
                info = classify(str(drive))
                places.append({"name": f"{letter}:", "path": str(drive), "group": "network" if info["kind"] == "network" else "volumes",
                               "kind": info["kind"], "is_network": info["kind"] == "network", "server": None, "share": None})
        candidates = [(home, "Home"), (home / "Desktop", "Desktop"), (home / "Pictures", "Pictures"), (home / "Videos", "Videos"), (home / "Documents", "Documents")]
    elif sys.platform == "darwin":
        candidates = [(home, "Home"), (home / "Desktop", "Desktop"), (home / "Pictures", "Pictures"), (home / "Movies", "Movies"), (home / "Documents", "Documents")]
    else:
        candidates = [(home, "Home"), (home / "Pictures", "Pictures"), (home / "Videos", "Videos"), (home / "Documents", "Documents")]
    places = [p for p in (_place(path, name, "places", mounts) for path, name in candidates) if p] + places
    seen = {p["path"] for p in places}
    parents = [Path("/Volumes")] if sys.platform == "darwin" else \
        [Path("/mnt"), Path("/media"), Path("/run/media") / os.environ.get("USER", ""), Path(f"/run/user/{os.getuid()}/gvfs")] if sys.platform != "win32" else []
    parents.append(home / "FaceHuntMounts")
    for parent in parents:
        try:
            children = sorted(p for p in parent.iterdir() if not p.name.startswith("."))
        except OSError:
            continue
        for child in children:
            ok, _ = run_with_timeout(child.is_dir, 2.0, False)
            if not ok or str(child) in seen:
                continue
            info = classify(str(child), mounts)
            if sys.platform == "darwin" and info["kind"] == "local":
                continue  # the startup disk appears under /Volumes too
            seen.add(str(child))
            places.append({"name": info.get("share") or child.name, "path": str(child), "group": "network" if info["kind"] == "network" else "volumes",
                           "kind": info["kind"], "is_network": info["kind"] == "network", "server": info.get("server"), "share": info.get("share")})
    for mount in mounts:
        if mount["network"] and mount["mount_point"] not in seen and os.path.isdir(mount["mount_point"]):
            seen.add(mount["mount_point"])
            places.append({"name": mount["share"] or os.path.basename(mount["mount_point"]), "path": mount["mount_point"], "group": "network",
                           "kind": "network", "is_network": True, "server": mount["server"], "share": mount["share"]})
    recent = [{"name": os.path.basename(p) or p, "path": p, "group": "recent", "kind": classify(p, mounts)["kind"],
               "is_network": classify(p, mounts)["kind"] == "network", "server": None, "share": None}
              for p in services().libraries.recent() if p not in {x["path"] for x in places if x["group"] == "places"}]
    return {"places": places + recent, "native_picker": bool(native_command()) and is_local_client(request),
            "separator": os.sep, "home": str(home), "shares": services().shares.capabilities()}


def _resolve(raw: str) -> Path:
    if not raw or "\x00" in raw:
        raise HTTPException(400, "A folder is required")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise HTTPException(400, "The folder must be an absolute path")
    resolved, error = run_with_timeout(lambda: path.resolve(strict=True), 5.0)
    if error is not None:
        if isinstance(error, TimeoutError):
            raise HTTPException(504, "This share is slow or unreachable. Retry.")
        raise HTTPException(404, "That folder does not exist any more")
    if not resolved.is_dir():
        raise HTTPException(400, "That is not a folder")
    return resolved


def _count(folder: str, deadline: float) -> Optional[dict]:
    photos = videos = 0
    try:
        with os.scandir(folder) as entries:
            for index, entry in enumerate(entries):
                if index % 256 == 0 and time.perf_counter() > deadline:
                    return None
                if is_junk_file(entry.name):
                    continue
                suffix = os.path.splitext(entry.name)[1].lower()
                photos += suffix in IMAGE
                videos += suffix in VIDEO
    except OSError:
        return None
    return {"photos": photos, "videos": videos}


@router.get("/api/fs/list")
def fs_list(request: Request, path: str, include_counts: bool = False):
    """Sub-folders of ``path`` (never files). Counts are direct children only and give up after 3 s per folder."""
    fs_guard(request)
    folder = _resolve(path)
    mounts = read_mounts()
    info = classify(str(folder), mounts)

    def read():
        out = []
        with os.scandir(folder) as entries:
            for entry in entries:
                if len(out) >= LIST_LIMIT:
                    break
                name = entry.name
                if is_junk_dir(name):
                    continue
                try:
                    if not entry.is_dir():  # follows links: a link to a folder is a folder
                        continue
                    target = os.path.realpath(entry.path) if entry.is_symlink() else entry.path
                except OSError:
                    continue
                out.append((name, target))
        return out

    listed, error = run_with_timeout(read, 10.0)
    if error is not None:
        if isinstance(error, PermissionError):
            raise HTTPException(403, "This folder cannot be opened. On macOS, allow Face Hunger (or Terminal) under System Settings > Privacy & Security > Files and Folders.")
        raise HTTPException(504, "This share is slow or unreachable. Retry.")
    items = []
    budget = time.perf_counter() + 12.0
    for name, target in sorted(listed, key=lambda pair: pair[0].casefold()):
        readable = os.access(target, os.R_OK | os.X_OK)
        has_children = None
        if readable:
            def peek(target=target):
                with os.scandir(target) as entries:
                    for index, entry in enumerate(entries):
                        if index > 400:
                            return None
                        if not is_junk_dir(entry.name) and entry.is_dir():
                            return True
                return False
            has_children, peek_error = run_with_timeout(peek, 1.5)
            if peek_error is not None:
                has_children = None
        item = {"name": name, "path": target, "is_readable": readable, "is_network": info["kind"] == "network", "has_children": has_children}
        if include_counts:
            counts = None
            if readable and time.perf_counter() < budget:
                counts, count_error = run_with_timeout(lambda target=target: _count(target, time.perf_counter() + COUNT_SECONDS), COUNT_SECONDS + 0.5)
                if count_error is not None:
                    counts = None
            item["counts"] = counts or "unknown"
        items.append(item)
    parent = str(folder.parent) if folder.parent != folder else None
    crumbs, cursor = [], folder
    while True:
        crumbs.append({"name": cursor.name or str(cursor), "path": str(cursor)})
        if cursor.parent == cursor:
            break
        cursor = cursor.parent
    return {"path": str(folder), "name": folder.name or str(folder), "parent": parent, "breadcrumbs": list(reversed(crumbs)),
            "kind": info["kind"], "is_network": info["kind"] == "network", "server": info.get("server"), "share": info.get("share"),
            "is_readable": os.access(folder, os.R_OK | os.X_OK), "items": items, "truncated": len(listed) >= LIST_LIMIT}


@router.get("/api/fs/summary")
def fs_summary(request: Request, path: str):
    """What adding this folder would mean: a capped estimate of photos and videos, and local free space."""
    fs_guard(request)
    folder = _resolve(path)
    info = classify(str(folder))
    deadline = time.perf_counter() + 4.0
    photos = videos = folders = 0
    complete = True
    stack = [str(folder)]
    while stack:
        if time.perf_counter() > deadline or folders > 5000:
            complete = False
            break
        current = stack.pop()
        folders += 1
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    name = entry.name
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if not is_junk_dir(name):
                                stack.append(entry.path)
                            continue
                    except OSError:
                        continue
                    if is_junk_file(name):
                        continue
                    suffix = os.path.splitext(name)[1].lower()
                    photos += suffix in IMAGE
                    videos += suffix in VIDEO
        except OSError:
            continue
    already = services().db.one("SELECT id, name FROM libraries WHERE path=?", (str(folder),))
    return {"path": str(folder), "kind": info["kind"], "is_network": info["kind"] == "network", "server": info.get("server"), "share": info.get("share"),
            "is_readable": os.access(folder, os.R_OK | os.X_OK), "photos": photos, "videos": videos, "complete": complete,
            "free_bytes_local": shutil.disk_usage(config.data_dir).free, "already_library": already}


@router.post("/api/fs/allow")
def fs_allow(body: FsAllowBody, request: Request):
    """The user confirmed these folders in the chooser: they become places libraries may live."""
    fs_guard(request)
    allowed = []
    for raw in body.paths[:50]:
        folder = _resolve(raw)
        services().libraries.allow(folder)
        allowed.append(str(folder))
    return {"allowed": allowed}


@router.post("/api/fs/native-pick")
def fs_native_pick(request: Request):
    """Open the system's folder dialog on the computer running the server (only for someone sitting at it)."""
    _require_csrf(request)
    if not is_local_client(request):
        raise HTTPException(403, "The system folder dialog can only be opened from the computer running Face Hunger")
    command = native_command()
    if command is None:
        raise HTTPException(501, "No system folder dialog is available here")
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        return {"cancelled": True}
    chosen = (done.stdout or "").strip()
    if done.returncode != 0 or not chosen:
        return {"cancelled": True}
    folder = _resolve(chosen)
    return {"cancelled": False, "path": str(folder)}
