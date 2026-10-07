"""Plugin manager API."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from ..deps import db, services
from ..jobs.manager import PRIORITY
from ..plugins.host import PluginError
from ..plugins.manifest import ManifestError
from ..schemas import PluginConfigureBody, PluginExportBody, PluginInstallBody, PluginPanelRpcBody
from ..services.plugins import PANEL_CSP
from ..services.presenters import _require_csrf

router = APIRouter()


def _guard(fn):
    try:
        return fn()
    except KeyError as exc:
        raise HTTPException(404, str(exc).strip("'")) from exc
    except ManifestError as exc:
        raise HTTPException(400, str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except PluginError as exc:
        raise HTTPException(403 if exc.denied else 502, f"Plugin error: {exc}") from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/api/plugins")
def list_plugins():
    return services().plugins.list()


@router.post("/api/plugins/install")
def install_plugin(body: PluginInstallBody, request: Request):
    """Copy a plugin from a local folder. It starts disabled, with no permissions."""
    _require_csrf(request)
    return _guard(lambda: services().plugins.install(body.path))


@router.patch("/api/plugins/{plugin_id}")
def configure_plugin(plugin_id: str, body: PluginConfigureBody, request: Request):
    _require_csrf(request)
    return _guard(lambda: services().plugins.configure(plugin_id, enabled=body.enabled, permissions=body.permissions))


@router.delete("/api/plugins/{plugin_id}")
def uninstall_plugin(plugin_id: str, request: Request):
    _require_csrf(request)
    return _guard(lambda: services().plugins.uninstall(plugin_id))


@router.post("/api/plugins/{plugin_id}/test")
def test_plugin(plugin_id: str, request: Request):
    """Start the plugin's process and ping it."""
    _require_csrf(request)
    return _guard(lambda: services().plugins.call(plugin_id, "ping", timeout=20))


@router.post("/api/plugins/{plugin_id}/export")
def plugin_export(plugin_id: str, body: PluginExportBody, request: Request):
    _require_csrf(request)
    ids = list(dict.fromkeys(body.media_ids or []))
    if body.album_id is not None:
        ids = [r["media_id"] for r in db.all("SELECT media_id FROM album_media WHERE album_id=? ORDER BY position, media_id", (body.album_id,))]
    if not ids:
        raise HTTPException(400, "Nothing to export")
    _guard(lambda: services().plugins._row(plugin_id))
    return services().jobs.enqueue("plugin_export", {"plugin_id": plugin_id, "media_ids": ids, "target_dir": body.target_dir,
                                                     "options": body.options or {}}, priority=PRIORITY["normal"])


@router.post("/api/plugins/{plugin_id}/classify")
def plugin_classify(plugin_id: str, request: Request):
    _require_csrf(request)
    _guard(lambda: services().plugins._row(plugin_id))
    return services().jobs.enqueue("plugin_classify", {"plugin_id": plugin_id}, priority=PRIORITY["background"],
                                   dedupe_key=f"plugin_classify:{plugin_id}")


@router.get("/api/media/{media_id}/plugin-labels")
def media_plugin_labels(media_id: int):
    return {"items": services().plugins.labels_for(media_id)}


@router.post("/api/plugins/{plugin_id}/panel-rpc")
def panel_rpc(plugin_id: str, body: PluginPanelRpcBody, request: Request):
    _require_csrf(request)
    return _guard(lambda: services().plugins.panel_rpc(plugin_id, body.method))


@router.get("/api/plugins/{plugin_id}/panel/{path:path}")
def panel_file(plugin_id: str, path: str = ""):
    target, media_type = _guard(lambda: services().plugins.panel_file(plugin_id, path))
    return FileResponse(target, media_type=media_type, headers={
        "Content-Security-Policy": PANEL_CSP, "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
        "Cache-Control": "no-store"})
