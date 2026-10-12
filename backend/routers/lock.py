"""Optional app lock: status, unlock, lock again, set/change/remove the password."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from ..deps import services
from ..schemas import LockPasswordBody, LockSetBody
from ..security import COOKIE, SESSION_SECONDS
from ..services.presenters import _require_csrf

router = APIRouter()


def _status(request: Request) -> dict:
    lock = services().lock
    return {"enabled": lock.enabled, "unlocked": (not lock.enabled) or lock.valid(request.cookies.get(COOKIE)),
            "managed_by_environment": lock.managed_by_environment,
            **({"face_enabled": True} if services().face_lock.enabled else {})}


def _check(password: str) -> None:
    lock = services().lock
    wait = lock.retry_after()
    if wait:
        raise HTTPException(429, f"Too many attempts. Try again in {wait} seconds.", headers={"Retry-After": str(wait)})
    if not lock.verify(password):
        raise HTTPException(401, "Wrong password")


def _start_session(request: Request, response: Response) -> None:
    response.set_cookie(COOKIE, services().lock.open_session(), max_age=None if services().face_lock.enabled else SESSION_SECONDS, httponly=True, samesite="strict",
                        secure=request.url.scheme == "https", path="/")


@router.get("/api/lock")
def lock_status(request: Request):
    return _status(request)


@router.post("/api/lock/unlock")
def unlock(body: LockPasswordBody, request: Request, response: Response):
    _require_csrf(request)
    if not services().lock.enabled:
        return _status(request)
    _check(body.password)
    _start_session(request, response)
    return {"enabled": True, "unlocked": True, "managed_by_environment": services().lock.managed_by_environment}


@router.post("/api/lock/lock")
def lock_again(request: Request, response: Response):
    _require_csrf(request)
    services().lock.close_session(request.cookies.get(COOKIE))
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


@router.put("/api/lock/password")
def set_password(body: LockSetBody, request: Request, response: Response):
    """Set or change the password. Changing needs the current one, and signs every session out."""
    _require_csrf(request)
    lock = services().lock
    if lock.enabled:
        _check(body.current or "")
    try:
        lock.set_password(body.password)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    _start_session(request, response)
    return {"enabled": True, "unlocked": True, "managed_by_environment": False}


@router.delete("/api/lock/password")
def remove_password(body: LockPasswordBody, request: Request, response: Response):
    _require_csrf(request)
    _check(body.password)
    try:
        services().lock.remove()
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    response.delete_cookie(COOKIE, path="/")
    return {"enabled": False, "unlocked": True, "managed_by_environment": False}


class FaceEnrollBody(BaseModel):
    password: str = Field(min_length=8, max_length=1024)
    images: list[str] = Field(min_length=3, max_length=3)


class FaceUnlockBody(BaseModel):
    challenge: str = Field(max_length=128)
    image: str = Field(max_length=2_000_000)


@router.post("/api/lock/face/enroll")
def enroll_face(body: FaceEnrollBody, request: Request, response: Response):
    _require_csrf(request)
    lock = services().lock
    if lock.enabled:
        _check(body.password)
    template = services().face_lock.enroll(body.images)
    if not lock.enabled:
        lock.set_password(body.password)
    services().face_lock.save(template)
    lock.invalidate_sessions()
    _start_session(request, response)
    return _status(request) | {"enabled": True, "unlocked": True, "face_enabled": True}


@router.post("/api/lock/face/challenge")
def face_challenge(request: Request):
    _require_csrf(request)
    wait = services().lock.retry_after()
    if wait:
        raise HTTPException(429, f"Try again in {wait} seconds")
    return {"challenge": services().face_lock.challenge()}


@router.post("/api/lock/face/unlock")
def unlock_face(body: FaceUnlockBody, request: Request, response: Response):
    _require_csrf(request)
    lock = services().lock
    wait = lock.retry_after()
    if wait:
        raise HTTPException(429, f"Try again in {wait} seconds")
    try:
        matched = services().face_lock.verify(body.challenge, body.image)
    except HTTPException:
        lock.record_face_attempt(False)
        raise
    lock.record_face_attempt(matched)
    if not matched:
        raise HTTPException(401, "Admin face did not match. App remains locked.")
    _start_session(request, response)
    return {"enabled": True, "unlocked": True, "face_enabled": True}


@router.delete("/api/lock/face")
def remove_face(body: LockPasswordBody, request: Request, response: Response):
    _require_csrf(request)
    _check(body.password)
    with services().db.connect() as conn:
        conn.execute("DELETE FROM settings WHERE key='admin_face_lock'")
    services().lock.invalidate_sessions()
    _start_session(request, response)
    return {"enabled": True, "unlocked": True, "face_enabled": False}
