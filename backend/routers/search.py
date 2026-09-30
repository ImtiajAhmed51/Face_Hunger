"""Search API routes."""

from __future__ import annotations

from fastapi import APIRouter, Request

from ..deps import db
from ..schemas import ParseSearchBody
from ..services.presenters import _require_csrf

router = APIRouter()

@router.post("/api/search/parse")
def parse_search(body: ParseSearchBody, request: Request):
    _require_csrf(request)
    tokens = body.query.strip().split()
    people: list[int] = []
    unmatched: list[str] = []
    mode = "ANY"
    date_from = date_to = kind = None

    for token in tokens:
        lower = token.lower()
        if lower in ("and", "all"):
            mode = "ALL"
            continue
        if lower in ("or", "any"):
            mode = "ANY"
            continue
        if lower in ("photo", "photos"):
            kind = "photo"
            continue
        if lower in ("video", "videos"):
            kind = "video"
            continue
        # try match person by name
        row = db.one(
            "SELECT id FROM people WHERE lower(name)=? AND face_count>0 LIMIT 1",
            (lower,),
        )
        if row:
            people.append(row["id"])
            continue
        # try partial
        rows = db.all(
            "SELECT id FROM people WHERE face_count>0 AND lower(name) LIKE ? ORDER BY face_count DESC LIMIT 3",
            (f"%{lower}%",),
        )
        if len(rows) == 1:
            people.append(rows[0]["id"])
        else:
            unmatched.append(token)

    return {
        "people": people,
        "mode": mode,
        "date_from": date_from,
        "date_to": date_to,
        "kind": kind,
        "unmatched": unmatched,
    }
