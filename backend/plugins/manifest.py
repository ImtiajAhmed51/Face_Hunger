"""``plugin.toml``: identity, API version, requested permissions and capabilities."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import API_VERSION

CAPABILITIES = {
    "embedding": "embed",          # embed(images: list[bytes]) -> list[list[float]]
    "classifier": "classify",      # classify(images: list[bytes]) -> list[list[{"label", "score"}]]
    "search_signal": "rank",       # rank(query: str, items: list[dict]) -> list[{"id", "score"}]
    "export": "plan",              # plan(items: list[dict], options: dict) -> list[{"id", "path"}]
    "ui_panel": None,              # static files shown in a sandboxed iframe
}
PERMISSIONS = {
    "network": "Connect to the network",
    "files.read": "Read the original files in your libraries",
    "storage": "Keep its own files in a private folder",
    "library.read": "See names of people and albums",
}
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,40}$")
ENTRY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_]*$")
MAX_FILES, MAX_BYTES = 5000, 200 * 1024 * 1024


class ManifestError(ValueError):
    pass


@dataclass
class Manifest:
    id: str
    name: str
    version: str
    api_version: str
    description: str = ""
    license: str = ""
    author: str = ""
    permissions: list[str] = field(default_factory=list)
    capabilities: dict[str, dict] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return dict(self.__dict__)

    @property
    def entries(self) -> dict[str, str]:
        return {name: cap["entry"] for name, cap in self.capabilities.items() if name != "ui_panel"}


def check_api_version(wanted: str, provided: str = API_VERSION) -> None:
    try:
        want = tuple(int(p) for p in str(wanted).split("."))[:2]
        have = tuple(int(p) for p in provided.split("."))[:2]
        if len(want) != 2:
            raise ValueError
    except ValueError:
        raise ManifestError(f"api_version must look like '1.0', got {wanted!r}") from None
    if want[0] != have[0] or want[1] > have[1]:
        raise ManifestError(f"This plugin needs plugin API {wanted}; this app provides {provided}. "
                            "Install a version of the plugin made for this app, or update the app.")


def parse(data: dict, folder: Path | None = None) -> Manifest:
    meta = data.get("plugin")
    if not isinstance(meta, dict):
        raise ManifestError("plugin.toml needs a [plugin] table")
    for key in ("id", "name", "version", "api_version"):
        if not isinstance(meta.get(key), str) or not meta[key].strip():
            raise ManifestError(f"plugin.toml: [plugin] {key} is required")
    if not ID_RE.match(meta["id"]):
        raise ManifestError("plugin.toml: id must be lowercase letters, digits and dashes (2-41 characters)")
    check_api_version(meta["api_version"])
    permissions = meta.get("permissions", [])
    if not isinstance(permissions, list) or any(p not in PERMISSIONS for p in permissions):
        unknown = [p for p in permissions if p not in PERMISSIONS] if isinstance(permissions, list) else permissions
        raise ManifestError(f"plugin.toml: unknown permission(s) {unknown}. Known: {sorted(PERMISSIONS)}")
    capabilities = data.get("capabilities") or {}
    if not isinstance(capabilities, dict) or not capabilities:
        raise ManifestError("plugin.toml: declare at least one [capabilities.<name>] table")
    for name, cap in capabilities.items():
        if name not in CAPABILITIES:
            raise ManifestError(f"plugin.toml: unknown capability '{name}'. Known: {sorted(CAPABILITIES)}")
        entry = cap.get("entry") if isinstance(cap, dict) else None
        if not isinstance(entry, str):
            raise ManifestError(f"plugin.toml: [capabilities.{name}] entry is required")
        if name == "ui_panel":
            relative = Path(entry)
            if relative.is_absolute() or ".." in relative.parts or relative.suffix.lower() != ".html":
                raise ManifestError("plugin.toml: ui_panel entry must be a relative .html file")
            if folder is not None and not (folder / relative).is_file():
                raise ManifestError(f"plugin.toml: ui_panel entry {entry} does not exist")
            continue
        if not ENTRY_RE.match(entry):
            raise ManifestError(f"plugin.toml: [capabilities.{name}] entry must look like 'module:attribute'")
        if folder is not None:
            module = entry.split(":")[0].replace(".", "/")
            if not ((folder / f"{module}.py").is_file() or (folder / module / "__init__.py").is_file()):
                raise ManifestError(f"plugin.toml: module for '{name}' ({entry}) is not in the plugin folder")
        if name == "embedding":
            dim = cap.get("dim")
            if not isinstance(dim, int) or not 2 <= dim <= 4096:
                raise ManifestError("plugin.toml: [capabilities.embedding] dim must be an integer between 2 and 4096")
            if not isinstance(cap.get("model_id"), str) or not re.match(r"^[A-Za-z0-9_.-]{1,60}$", cap["model_id"]):
                raise ManifestError("plugin.toml: [capabilities.embedding] model_id is required (letters, digits, . _ -)")
    return Manifest(id=meta["id"], name=meta["name"].strip()[:80], version=meta["version"].strip()[:40],
                    api_version=meta["api_version"], description=str(meta.get("description", ""))[:500],
                    license=str(meta.get("license", ""))[:80], author=str(meta.get("author", ""))[:120],
                    permissions=list(dict.fromkeys(permissions)), capabilities=capabilities)


def load(folder: Path) -> Manifest:
    """Read and validate a plugin folder without importing any of its code."""
    folder = Path(folder)
    path = folder / "plugin.toml"
    if not folder.is_dir() or not path.is_file():
        raise ManifestError("That folder has no plugin.toml")
    files = total = 0
    for item in folder.rglob("*"):
        if item.is_symlink():
            raise ManifestError(f"Plugins may not contain symbolic links ({item.name})")
        if item.is_file():
            files += 1
            total += item.stat().st_size
            if files > MAX_FILES or total > MAX_BYTES:
                raise ManifestError("The plugin folder is too large (limit: 5000 files, 200 MB)")
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise ManifestError(f"plugin.toml is not valid TOML: {exc}") from exc
    return parse(data, folder)
