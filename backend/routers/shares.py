"""Network shares: list what is connected, connect, disconnect, discover."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from ..deps import services
from ..schemas import ShareConnectBody, ShareDisconnectBody
from ..services.shares import ShareError
from .fs import fs_guard

router = APIRouter()


def _fail(exc: ShareError, status: int = 400):
    raise HTTPException(status, exc.to_dict()) from exc


@router.get("/api/shares")
def list_shares(request: Request):
    fs_guard(request)
    return {"items": services().shares.list(), "capabilities": services().shares.capabilities()}


@router.post("/api/shares/connect")
def connect_share(body: ShareConnectBody, request: Request):
    """Mount a share for this user (no sudo). The password is used for this call only unless "remember" is set."""
    fs_guard(request)
    try:
        return services().shares.connect(protocol=body.protocol, server=body.server, share=body.share, username=body.username,
                                         password=body.password.get_secret_value() if body.password else None,
                                         guest=body.guest, remember=body.remember)
    except ShareError as exc:
        _fail(exc, {"auth": 401, "permission": 403, "share_not_found": 404, "timeout": 504, "unreachable": 502, "firewall": 502,
                    "unsupported": 501}.get(exc.code, 400))


@router.post("/api/shares/disconnect")
def disconnect_share(body: ShareDisconnectBody, request: Request):
    fs_guard(request)
    try:
        result = services().shares.disconnect(body.mount_point, force=body.force)
    except ShareError as exc:
        _fail(exc, 409 if exc.code == "already" else 400)
    services().libraries.tick()
    return result


@router.get("/api/shares/discover")
def discover_shares(request: Request):
    fs_guard(request)
    return {"items": services().shares.discover()}
