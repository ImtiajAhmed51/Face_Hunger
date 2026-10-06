"""Search API routes: query parsing, hybrid search and saved searches."""

from __future__ import annotations

import calendar
import json
from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter, HTTPException, Request

from ..deps import db, models, services
from ..schemas import HybridSearchBody, ParseSearchBody, SavedSearchBody, SavedSearchRunBody
from ..services.presenters import _now, _require_csrf

router = APIRouter()

MONTHS = {name.lower(): i for i, name in enumerate(calendar.month_name) if name}
MONTHS.update({name.lower(): i for i, name in enumerate(calendar.month_abbr) if name})
DATE_PREPOSITIONS = {"in", "from", "during", "on", "since", "after", "before", "until"}
STOPWORDS = {"a", "an", "the", "of", "with", "and", "or", "at", "to", "in", "on", "me", "my"}


def _year(token: str) -> Optional[int]:
    if token.isdigit() and len(token) == 4 and 1900 <= int(token) <= 2100:
        return int(token)
    return None


# Northern-hemisphere meteorological seasons (documented in DECISIONS: no location-based guess).
SEASONS = {"spring": (3, 5), "summer": (6, 8), "autumn": (9, 11), "fall": (9, 11), "winter": (12, 2)}


def _season_range(season: str, year: int) -> tuple[str, str]:
    start, end = SEASONS[season]
    end_year = year + 1 if end < start else year  # winter 2023 = Dec 2023 - Feb 2024
    return f"{year:04d}-{start:02d}-01", f"{end_year:04d}-{end:02d}-{calendar.monthrange(end_year, end)[1]:02d}"


def _month_range(year: int, month: int) -> tuple[str, str]:
    last = calendar.monthrange(year, month)[1]
    return f"{year:04d}-{month:02d}-01", f"{year:04d}-{month:02d}-{last:02d}"


def parse_dates(tokens: list[str], today: date) -> tuple[Optional[str], Optional[str], list[bool]]:
    """Return (date_from, date_to, consumed-mask) for natural date phrases."""
    used = [False] * len(tokens)
    lower = [t.lower().strip(",.") for t in tokens]
    date_from = date_to = None

    def span(start: str, end: str, preposition: Optional[str]):
        nonlocal date_from, date_to
        if preposition in ("since", "after", "from"):
            date_from = start
        elif preposition in ("before", "until"):
            date_to = (date.fromisoformat(start) - timedelta(days=1)).isoformat()
        else:
            date_from, date_to = start, end

    i = 0
    while i < len(lower):
        prep = lower[i - 1] if i > 0 and lower[i - 1] in DATE_PREPOSITIONS and not used[i - 1] else None
        word, nxt = lower[i], lower[i + 1] if i + 1 < len(lower) else ""
        matched = 0
        if word in ("today", "yesterday"):
            d = today if word == "today" else today - timedelta(days=1)
            span(d.isoformat(), d.isoformat(), prep)
            matched = 1
        elif word in ("last", "this") and nxt in ("year", "month", "week"):
            if nxt == "year":
                y = today.year - (word == "last")
                span(f"{y}-01-01", f"{y}-12-31", prep)
            elif nxt == "month":
                first = today.replace(day=1)
                ref = (first - timedelta(days=1)).replace(day=1) if word == "last" else first
                span(*_month_range(ref.year, ref.month), prep)
            else:
                monday = today - timedelta(days=today.weekday())
                start = monday - timedelta(days=7) if word == "last" else monday
                span(start.isoformat(), (start + timedelta(days=6)).isoformat(), prep)
            matched = 2
        elif word in ("last", "this") and nxt in SEASONS:
            year = today.year - (word == "last")
            span(*_season_range(nxt, year), prep)
            matched = 2
        elif word in SEASONS:
            y = _year(nxt)
            # A bare season means the most recent one that has started.
            year = y or (today.year if date.fromisoformat(_season_range(word, today.year)[0]) <= today else today.year - 1)
            span(*_season_range(word, year), prep)
            matched = 2 if y else 1
        elif word in MONTHS and len(word) >= 3:
            y = _year(nxt)
            month = MONTHS[word]
            year = y or (today.year if month <= today.month else today.year - 1)
            span(*_month_range(year, month), prep)
            matched = 2 if y else 1
        elif _year(word):
            span(f"{word}-01-01", f"{word}-12-31", prep)
            matched = 1
        if matched:
            for j in range(i, i + matched):
                used[j] = True
            if prep:
                used[i - 1] = True
            i += matched
        else:
            i += 1
    return date_from, date_to, used


def _match_people(tokens: list[str], used: list[bool], database=None) -> tuple[list[int], list[bool]]:
    """Greedy longest-phrase match of person names (up to 3 words), then single-word partials."""
    database = database if database is not None else db
    people: list[int] = []
    i = 0
    while i < len(tokens):
        if used[i]:
            i += 1
            continue
        hit = None
        for width in (3, 2, 1):
            if i + width > len(tokens) or any(used[i:i + width]):
                continue
            phrase = " ".join(tokens[i:i + width]).lower()
            row = database.one("SELECT id FROM people WHERE lower(name)=? AND face_count>0 LIMIT 1", (phrase,))
            if row:
                hit = (row["id"], width)
                break
        if hit is None:
            lower = tokens[i].lower()
            if len(lower) > 2 and lower not in STOPWORDS:
                rows = database.all(
                    "SELECT id FROM people WHERE face_count>0 AND lower(name) LIKE ? ORDER BY face_count DESC LIMIT 3",
                    (f"%{lower}%",),
                )
                if len(rows) == 1:
                    hit = (rows[0]["id"], 1)
        if hit:
            if hit[0] not in people:
                people.append(hit[0])
            for j in range(i, i + hit[1]):
                used[j] = True
            i += hit[1]
        else:
            i += 1
    return people, used


def parse_query(query: str, today: Optional[date] = None, database=None) -> dict:
    tokens = query.strip().split()
    mode = "ANY"
    kind = None
    date_from, date_to, used = parse_dates(tokens, today or date.today())
    for i, token in enumerate(tokens):
        if used[i]:
            continue
        lower = token.lower()
        if lower in ("and", "all"):
            mode, used[i] = "ALL", True
        elif lower in ("or", "any"):
            mode, used[i] = "ANY", True
        elif lower in ("photo", "photos"):
            kind, used[i] = "photo", True
        elif lower in ("video", "videos"):
            kind, used[i] = "video", True
    people, used = _match_people(tokens, used, database)
    unmatched = [t for t, u in zip(tokens, used) if not u]
    text = " ".join(t for t in unmatched if t.lower() not in STOPWORDS or len(unmatched) > 1).strip()
    return {
        "people": people,
        "mode": mode,
        "date_from": date_from,
        "date_to": date_to,
        "kind": kind,
        "unmatched": unmatched,
        "text": text,
    }


@router.post("/api/search/parse")
def parse_search(body: ParseSearchBody, request: Request):
    """Parse a free-form query. The original keys are unchanged; ``filters`` and
    ``embedding_query`` are ready to post to /api/search/hybrid."""
    _require_csrf(request)
    parsed = parse_query(body.query)
    encoder = models.text_encoder()
    parsed["filters"] = {
        "people": parsed["people"], "people_mode": parsed["mode"], "kind": parsed["kind"],
        "date_from": parsed["date_from"], "date_to": parsed["date_to"],
    }
    parsed["embedding_query"] = {
        "text": parsed["text"],
        "model": encoder.spec.key if encoder else None,
        "available": bool(encoder and parsed["text"]),
    }
    if body.embed and encoder and parsed["text"]:
        parsed["embedding_query"]["vector"] = [round(float(x), 6) for x in encoder.embed_texts([parsed["text"]])[0]]
    return parsed


@router.post("/api/search/hybrid")
def hybrid_search(body: HybridSearchBody, request: Request):
    _require_csrf(request)
    return services().search.run(body.model_dump())


def _saved_row(row: dict) -> dict:
    return {"id": row["id"], "name": row["name"], "query": json.loads(row["query"]),
            "created_at": row["created_at"], "last_run_at": row["last_run_at"], "run_count": row["run_count"]}


@router.get("/api/search/saved")
def list_saved_searches():
    return {"items": [_saved_row(r) for r in db.all("SELECT * FROM saved_searches ORDER BY name COLLATE NOCASE, id")]}


@router.post("/api/search/saved")
def create_saved_search(body: SavedSearchBody, request: Request):
    _require_csrf(request)
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "name required")
    query = body.query.model_dump(exclude={"page", "limit"}, exclude_none=True)
    if not query.get("expansions"):
        query.pop("expansions", None)
    with db.connect() as conn:
        sid = conn.execute("INSERT INTO saved_searches(name, query) VALUES (?,?)", (name, json.dumps(query))).lastrowid
    return _saved_row(db.one("SELECT * FROM saved_searches WHERE id=?", (sid,)))


@router.delete("/api/search/saved/{search_id}")
def delete_saved_search(search_id: int, request: Request):
    _require_csrf(request)
    with db.connect() as conn:
        if not conn.execute("DELETE FROM saved_searches WHERE id=?", (search_id,)).rowcount:
            raise HTTPException(404, "Saved search not found")
    return {"ok": True}


@router.post("/api/search/saved/{search_id}/run")
def run_saved_search(search_id: int, request: Request, body: Optional[SavedSearchRunBody] = None):
    _require_csrf(request)
    row = db.one("SELECT * FROM saved_searches WHERE id=?", (search_id,))
    if not row:
        raise HTTPException(404, "Saved search not found")
    query = HybridSearchBody(**json.loads(row["query"])).model_dump()
    if body is not None:
        query.update(page=body.page, limit=body.limit)
    with db.connect() as conn:
        conn.execute("UPDATE saved_searches SET last_run_at=?, run_count=run_count+1 WHERE id=?", (_now(), search_id))
    return {**services().search.run(query), "saved_search": _saved_row(db.one("SELECT * FROM saved_searches WHERE id=?", (search_id,)))}
