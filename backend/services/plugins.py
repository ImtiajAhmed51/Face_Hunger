"""Plugin manager: install from a local folder, permissions review, isolated execution.

Installed plugins are copied into ``data_dir/plugins/<id>``; their code is never imported
by the server. Every call goes through a :class:`PluginProcess` (a sandboxed subprocess),
so a broken plugin costs one failed call, not the server.
"""

from __future__ import annotations

import base64
import json
import logging
import mimetypes
import re
import shutil
import threading
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from ..plugins import API_VERSION
from ..plugins.host import PluginError, PluginProcess, kernel_sandbox_available
from ..plugins.manifest import PERMISSIONS, Manifest, ManifestError, load, parse
from ..vectors.specs import ModelSpec

logger = logging.getLogger(__name__)

PANEL_TYPES = {".html", ".js", ".css", ".json", ".png", ".jpg", ".jpeg", ".svg", ".webp", ".woff2"}
PANEL_CSP = ("default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
             "img-src 'self' data: blob:; font-src 'self'; connect-src 'none'; form-action 'none'; base-uri 'none'; sandbox allow-scripts")
MAX_CRASHES = 3
SAFE_PART = re.compile(r'[<>:"|?*\x00-\x1f\\]')


def _jpeg(bgr: np.ndarray, side: int = 384) -> str:
    import cv2

    h, w = bgr.shape[:2]
    scale = side / max(h, w)
    if scale < 1:
        bgr = cv2.resize(bgr, (max(1, round(w * scale)), max(1, round(h * scale))), interpolation=cv2.INTER_AREA)
    ok, data = cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, 88])
    if not ok:
        raise ValueError("could not encode image for the plugin")
    return base64.b64encode(data.tobytes()).decode()


class PluginService:
    def __init__(self, services):
        self.s = services
        self.root = Path(services.config.data_dir) / "plugins"
        self.storage_root = Path(services.config.data_dir) / "plugin-data"
        self._processes: dict[str, PluginProcess] = {}
        self._lock = threading.Lock()

    @property
    def db(self):
        return self.s.db

    # -- registry ---------------------------------------------------------------------
    def _row(self, plugin_id: str) -> dict:
        row = self.db.one("SELECT * FROM plugins WHERE id=?", (plugin_id,))
        if not row:
            raise KeyError("Plugin not found")
        return row

    def _manifest(self, row: dict) -> Manifest:
        return Manifest(**json.loads(row["manifest"]))

    def _present(self, row: dict) -> dict:
        manifest = json.loads(row["manifest"])
        process = self._processes.get(row["id"])
        return {"id": row["id"], "name": manifest["name"], "version": manifest["version"], "api_version": manifest["api_version"],
                "description": manifest["description"], "license": manifest["license"], "author": manifest["author"],
                "capabilities": {name: {k: v for k, v in cap.items() if k != "entry"} for name, cap in manifest["capabilities"].items()},
                "requested_permissions": manifest["permissions"], "granted_permissions": json.loads(row["granted_permissions"]),
                "enabled": bool(row["enabled"]), "installed_at": row["installed_at"], "last_error": row["last_error"],
                "process": process.status() if process else None}

    def list(self) -> dict:
        rows = self.db.all("SELECT * FROM plugins ORDER BY id")
        return {"api_version": API_VERSION, "kernel_sandbox": kernel_sandbox_available(),
                "permissions": PERMISSIONS, "items": [self._present(r) for r in rows], "available": self.discover()}

    def discover(self) -> list[dict]:
        """Plugins shipped as Python packages (entry point group ``facehunger.plugins``), not yet installed."""
        from importlib.metadata import entry_points

        installed = {r["id"] for r in self.db.all("SELECT id FROM plugins")}
        found = []
        try:
            points = entry_points(group="facehunger.plugins")
        except Exception:
            return []
        for point in points:
            try:
                folder = Path(point.dist.locate_file(point.value.split(":")[0].replace(".", "/")))
                manifest = load(folder)
            except Exception:
                continue
            if manifest.id not in installed:
                found.append({"id": manifest.id, "name": manifest.name, "version": manifest.version, "path": str(folder)})
        return found

    def install(self, source: str) -> dict:
        folder = Path(source).expanduser()
        if not folder.is_absolute():
            raise ManifestError("Give the full path of the plugin folder")
        manifest = load(folder)
        if self.db.one("SELECT 1 AS x FROM plugins WHERE id=?", (manifest.id,)):
            raise ManifestError(f"A plugin with the id '{manifest.id}' is already installed. Uninstall it first.")
        target = self.root / manifest.id
        if target.exists():
            shutil.rmtree(target)
        self.root.mkdir(parents=True, exist_ok=True)
        shutil.copytree(folder, target, symlinks=False, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".git"))
        with self.db.connect() as conn:
            conn.execute("INSERT INTO plugins(id, manifest, source_path) VALUES (?,?,?)",
                         (manifest.id, json.dumps(manifest.to_dict()), str(folder)))
        logger.info("Plugin %s %s installed (disabled, no permissions)", manifest.id, manifest.version)
        return self._present(self._row(manifest.id))

    def configure(self, plugin_id: str, *, enabled: Optional[bool] = None, permissions: Optional[list[str]] = None) -> dict:
        row = self._row(plugin_id)
        manifest = self._manifest(row)
        granted = json.loads(row["granted_permissions"])
        if permissions is not None:
            extra = [p for p in permissions if p not in manifest.permissions]
            if extra:
                raise ManifestError(f"The plugin did not ask for: {', '.join(extra)}")
            granted = [p for p in manifest.permissions if p in permissions]
        if enabled:
            # Re-validate what is on disk: the manifest stored at install time is what runs.
            parse({"plugin": {k: getattr(manifest, k) for k in ("id", "name", "version", "api_version", "permissions")},
                   "capabilities": manifest.capabilities}, self.root / plugin_id)
        with self.db.connect() as conn:
            conn.execute("UPDATE plugins SET enabled=COALESCE(?, enabled), granted_permissions=?, last_error=NULL WHERE id=?",
                         (None if enabled is None else int(enabled), json.dumps(granted), plugin_id))
        self._stop(plugin_id)  # permissions are fixed at process start
        self._sync(self._row(plugin_id))
        return self._present(self._row(plugin_id))

    def uninstall(self, plugin_id: str) -> dict:
        row = self._row(plugin_id)
        self._stop(plugin_id)
        self._unregister(self._manifest(row))
        with self.db.connect() as conn:
            conn.execute("DELETE FROM plugin_labels WHERE plugin_id=?", (plugin_id,))
            conn.execute("DELETE FROM plugin_media_done WHERE plugin_id=?", (plugin_id,))
            conn.execute("DELETE FROM plugins WHERE id=?", (plugin_id,))
        for folder in (self.root / plugin_id, self.storage_root / plugin_id):
            shutil.rmtree(folder, ignore_errors=True)
        (self.root / f"{plugin_id}.log").unlink(missing_ok=True)
        return {"ok": True, "id": plugin_id}

    # -- processes --------------------------------------------------------------------
    def _config(self, row: dict) -> dict:
        manifest = self._manifest(row)
        granted = json.loads(row["granted_permissions"])
        storage = self.storage_root / row["id"]
        if "storage" in granted:
            storage.mkdir(parents=True, exist_ok=True)
        return {"plugin_dir": str(self.root / row["id"]), "entries": manifest.entries, "permissions": granted,
                "storage_dir": str(storage), "api_version": API_VERSION,
                "read_roots": [r["path"] for r in self.db.all("SELECT path FROM libraries")]}

    def _process(self, plugin_id: str) -> PluginProcess:
        with self._lock:
            process = self._processes.get(plugin_id)
            if process is None:
                row = self._row(plugin_id)
                if not row["enabled"]:
                    raise PluginError("This plugin is disabled")
                process = PluginProcess(plugin_id, self._config(row), log_path=self.root / f"{plugin_id}.log",
                                        protected=[self.s.config.data_dir])
                self._processes[plugin_id] = process
            return process

    def _stop(self, plugin_id: str) -> None:
        with self._lock:
            process = self._processes.pop(plugin_id, None)
        if process:
            process.stop()

    def call(self, plugin_id: str, method: str, params: Optional[dict] = None, *, timeout: float = 30.0):
        process = self._process(plugin_id)
        try:
            return process.call(method, params, timeout=timeout)
        except PluginError as exc:
            if exc.crashed and process.crashes >= MAX_CRASHES:
                # Stop a crash loop: the plugin stays installed but is switched off until the user re-enables it.
                with self.db.connect() as conn:
                    conn.execute("UPDATE plugins SET enabled=0, last_error=? WHERE id=?",
                                 (f"Disabled after {process.crashes} crashes: {exc}", plugin_id))
                self._stop(plugin_id)
                self._unregister(self._manifest(self._row(plugin_id)))
            else:
                with self.db.connect() as conn:
                    conn.execute("UPDATE plugins SET last_error=? WHERE id=?", (str(exc)[:500], plugin_id))
            raise

    def close(self) -> None:
        for plugin_id in list(self._processes):
            self._stop(plugin_id)

    # -- extension point: embedding models ---------------------------------------------
    def _spec(self, manifest: Manifest) -> Optional[ModelSpec]:
        cap = manifest.capabilities.get("embedding")
        if not cap:
            return None
        return ModelSpec(f"plugin-{manifest.id}-{cap['model_id']}", str(cap.get("version", manifest.version)), int(cap["dim"]),
                         "media", "visual")

    def _sync(self, row: dict) -> None:
        """Make the registry of embedders match the plugin's enabled state."""
        from ..ml.models import MediaEmbedder

        manifest = self._manifest(row)
        spec = self._spec(manifest)
        if spec is None:
            return
        if not row["enabled"]:
            self._unregister(manifest)
            return
        plugin_id = row["id"]

        def embed_images(images) -> np.ndarray:
            out = []
            for start in range(0, len(images), 8):
                batch = [_jpeg(img) for img in images[start:start + 8]]
                vectors = np.asarray(self.call(plugin_id, "embedding.embed", {"images_b64": batch}, timeout=120), dtype=np.float32)
                if vectors.shape != (len(batch), spec.dim) or not np.isfinite(vectors).all():
                    raise PluginError(f"The plugin returned vectors of shape {vectors.shape}, expected ({len(batch)}, {spec.dim})")
                out.append(vectors)
            return np.concatenate(out) if out else np.zeros((0, spec.dim), np.float32)

        self.s.extra_embedders[spec.key] = MediaEmbedder(spec, embed_images)
        self.s.vectors.register(spec)

    def _unregister(self, manifest: Manifest) -> None:
        spec = self._spec(manifest)
        if spec is None:
            return
        self.s.extra_embedders.pop(spec.key, None)
        # If search was pinned to this plugin's model, fall back to the built-in one. Its vectors stay on disk.
        settings = self.db.settings()
        if (settings.get("active_models") or {}).get(spec.role) == spec.key:
            self.s.storage._pin(spec.role, None)
        if (settings.get("model_upgrade") or {}).get("to_key") == spec.key:
            self.s.storage.finish()

    def activate(self) -> None:
        for row in self.db.all("SELECT * FROM plugins WHERE enabled=1"):
            try:
                self._sync(row)
            except Exception as exc:
                logger.warning("Plugin %s could not be activated: %s", row["id"], exc)

    def enabled_with(self, capability: str) -> list[dict]:
        rows = self.db.all("SELECT * FROM plugins WHERE enabled=1 ORDER BY id")
        return [r for r in rows if capability in json.loads(r["manifest"])["capabilities"]]

    # -- extension point: classifiers / detectors ---------------------------------------
    def classify(self, plugin_id: str, *, checkpoint: Callable = lambda: None, progress: Callable = lambda **_: None,
                 batch_size: int = 8) -> dict:
        from ..ml.models import _media_frames

        if "classifier" not in self._manifest(self._row(plugin_id)).capabilities:
            raise PluginError("This plugin has no classifier")
        total = self.db.one("SELECT COUNT(*) AS n FROM media WHERE deleted_at IS NULL AND missing=0 AND kind='photo'")["n"]
        done = labelled = failed = 0
        while True:
            checkpoint()
            rows = self.db.all("""SELECT m.* FROM media m WHERE m.deleted_at IS NULL AND m.missing=0 AND m.kind='photo'
                                  AND NOT EXISTS (SELECT 1 FROM plugin_media_done d WHERE d.plugin_id=? AND d.media_id=m.id)
                                  ORDER BY m.id LIMIT ?""", (plugin_id, batch_size))
            if not rows:
                break
            images, owners = [], []
            for row in rows:
                try:
                    images.append(_jpeg(_media_frames(row)[0]))
                    owners.append(row["id"])
                except Exception:
                    failed += 1
            results = self.call(plugin_id, "classifier.classify", {"images_b64": images}, timeout=120) if images else []
            if not isinstance(results, list) or len(results) != len(images):
                raise PluginError("The classifier must return one list of labels per image")
            with self.db.connect() as conn:
                for media_id, labels in zip(owners, results):
                    for item in (labels or [])[:20]:
                        label = str(item.get("label", "")).strip().lower()[:60]
                        score = float(item.get("score", 0.0))
                        if label and np.isfinite(score):
                            conn.execute("INSERT OR REPLACE INTO plugin_labels(plugin_id, media_id, label, score) VALUES (?,?,?,?)",
                                         (plugin_id, media_id, label, max(0.0, min(1.0, score))))
                            labelled += 1
                conn.executemany("INSERT OR IGNORE INTO plugin_media_done(plugin_id, media_id) VALUES (?,?)",
                                 [(plugin_id, r["id"]) for r in rows])
            done += len(rows)
            progress(processed=done, total=total, labels=labelled)
        return {"processed": done, "labels": labelled, "failed": failed, "total": total}

    def label_ranked(self, text: str, allowed, want: int) -> list[tuple[int, float]]:
        words = [w for w in re.findall(r"\w+", text.lower()) if len(w) > 2][:8]
        if not words:
            return []
        rows = self.db.all(f"""SELECT l.media_id, MAX(l.score) AS score FROM plugin_labels l JOIN plugins p ON p.id=l.plugin_id AND p.enabled=1
                               WHERE l.label IN ({','.join('?' * len(words))}) GROUP BY l.media_id ORDER BY score DESC, l.media_id LIMIT ?""",
                           (*words, want))
        keep = None if allowed is None else set(int(i) for i in allowed)
        return [(r["media_id"], float(r["score"])) for r in rows if keep is None or r["media_id"] in keep]

    def labels_for(self, media_id: int) -> list[dict]:
        return self.db.all("SELECT plugin_id, label, score FROM plugin_labels WHERE media_id=? ORDER BY score DESC", (media_id,))

    # -- extension point: search signals -------------------------------------------------
    def search_signals(self, text: str, candidate_ids: list[int]) -> tuple[dict[str, list[tuple[int, float]]], list[str]]:
        ranked, warnings = {}, []
        plugins = self.enabled_with("search_signal")
        if not plugins or not candidate_ids:
            return ranked, warnings
        ids = candidate_ids[:300]
        rows = self.db.all(f"SELECT id, name, kind, captured_at, width, height FROM media WHERE id IN ({','.join('?' * len(ids))})", tuple(ids))
        known = {r["id"] for r in rows}
        for row in plugins:
            try:
                result = self.call(row["id"], "search_signal.rank", {"query": text, "items": rows}, timeout=2.0)
                pairs = [(int(r["id"]), float(r["score"])) for r in result if int(r["id"]) in known and np.isfinite(float(r["score"]))]
                if pairs:
                    ranked[f"plugin_{row['id']}"] = sorted(pairs, key=lambda p: -p[1])
            except Exception as exc:
                warnings.append(f"Plugin '{row['id']}' search signal failed: {str(exc)[:160]}")
        return ranked, warnings

    # -- extension point: export targets -----------------------------------------------
    def _items(self, media_ids: list[int], *, names: bool) -> list[dict]:
        if not media_ids:
            return []
        marks = ",".join("?" * len(media_ids))
        rows = self.db.all(f"SELECT id, name, kind, captured_at, path FROM media WHERE deleted_at IS NULL AND missing=0 AND id IN ({marks})",
                           tuple(media_ids))
        people: dict[int, list] = {}
        for r in self.db.all(f"""SELECT DISTINCT f.media_id, p.id AS person_id, p.name FROM faces f JOIN people p ON p.id=f.person_id
                                 WHERE f.media_id IN ({marks}) ORDER BY p.id""", tuple(media_ids)):
            # Without library.read the plugin only gets opaque ids, never names.
            people.setdefault(r["media_id"], []).append(r["name"] if names and r["name"] else f"person-{r['person_id']}")
        return [{"id": r["id"], "name": r["name"], "kind": r["kind"], "captured_at": r["captured_at"],
                 "people": people.get(r["id"], []), "_path": r["path"]} for r in rows]

    def export(self, plugin_id: str, media_ids: list[int], target_dir: str, *, options: Optional[dict] = None,
               checkpoint: Callable = lambda: None, progress: Callable = lambda **_: None) -> dict:
        row = self._row(plugin_id)
        if "export" not in self._manifest(row).capabilities:
            raise PluginError("This plugin has no export target")
        try:
            folder = self.s.sharing._folder(target_dir)
        except Exception as exc:
            raise ManifestError(str(exc)) from exc
        items = self._items(media_ids, names="library.read" in json.loads(row["granted_permissions"]))
        sources = {i["id"]: Path(i.pop("_path")) for i in items}
        plan = self.call(plugin_id, "export.plan", {"items": items, "options": options or {}}, timeout=120)
        if not isinstance(plan, list) or len(plan) > max(1, len(items)) * 10:
            raise PluginError("The export plugin returned an invalid plan")
        written = skipped = 0
        for index, entry in enumerate(plan):
            checkpoint()
            try:
                source = sources[int(entry["id"])]
                relative = Path(str(entry["path"]))
            except (KeyError, TypeError, ValueError):
                skipped += 1
                continue
            # The plugin only proposes names. The host writes, and only inside the chosen folder.
            if relative.is_absolute() or not relative.parts or any(p in ("..", ".") or SAFE_PART.search(p) or len(p) > 200 for p in relative.parts) \
                    or len(relative.parts) > 8:
                skipped += 1
                continue
            target = (folder / relative).resolve()
            if not target.is_relative_to(folder) or not source.is_file():
                skipped += 1
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            n = 1
            while target.exists():  # new files only: never overwrite
                target = target.with_name(f"{relative.stem}-{n}{relative.suffix}")
                n += 1
            shutil.copy2(source, target)
            written += 1
            progress(processed=index + 1, total=len(plan))
        return {"written": written, "skipped": skipped, "target": str(folder), "processed": len(plan), "total": len(plan)}

    # -- extension point: UI panels ------------------------------------------------------
    def panel_file(self, plugin_id: str, relative: str) -> tuple[Path, str]:
        row = self._row(plugin_id)
        panel = self._manifest(row).capabilities.get("ui_panel")
        if not row["enabled"] or not panel:
            raise KeyError("No panel")
        base = (self.root / plugin_id / Path(panel["entry"]).parent).resolve()
        target = (base / (relative or Path(panel["entry"]).name)).resolve()
        if not target.is_relative_to(base) or not target.is_file() or target.suffix.lower() not in PANEL_TYPES:
            raise KeyError("Not found")
        return target, mimetypes.guess_type(target.name)[0] or "application/octet-stream"

    def panel_rpc(self, plugin_id: str, method: str) -> dict:
        """The message API behind a panel's postMessage calls. Permissions are checked here, on the server."""
        row = self._row(plugin_id)
        if not row["enabled"] or "ui_panel" not in self._manifest(row).capabilities:
            raise KeyError("No panel")
        granted = json.loads(row["granted_permissions"])
        if method == "host.info":
            return {"api_version": API_VERSION, "plugin": plugin_id, "permissions": granted}
        if method == "library.summary":
            counts = self.db.one("""SELECT COALESCE(SUM(kind='photo'), 0) AS photos, COALESCE(SUM(kind='video'), 0) AS videos
                                    FROM media WHERE deleted_at IS NULL AND missing=0""")
            return {**counts, "people": self.db.one("SELECT COUNT(*) AS n FROM people WHERE face_count>0")["n"]}
        if method == "library.people":
            if "library.read" not in granted:
                raise PermissionError("This panel needs the 'library.read' permission")
            return {"items": self.db.all("SELECT name, face_count FROM people WHERE face_count>0 AND name IS NOT NULL "
                                         "ORDER BY face_count DESC LIMIT 200")}
        raise ValueError(f"Unknown panel method '{method}'")
