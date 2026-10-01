"""Event / moment detection with user overrides that survive re-detection.

Detection (pure, see ``cluster``):
  1. Sort by capture time and link consecutive items into segments when the
     gap is small (<= 4 h) and, if both have GPS, the implied travel is
     plausible (<= 150 km apart and <= 300 km/h).
  2. Trips: consecutive segments whose items are all away from "home" (the
     densest GPS cell of the library) and within 400 km of each other merge
     across nights (gap <= 36 h). Days at home stay separate events.
  3. Visual coherence (DINOv2, when vectors exist): inside a segment, a gap of
     >= 90 min where the visual centroids before/after are dissimilar
     (cos < 0.45) splits the segment.
  4. People overlap: adjacent segments within 12 h at the same place whose
     people sets overlap (Jaccard >= 0.5) merge.

Overrides: every user edit locks the media it touches (``event_media.locked``)
and marks the event ``user_edited``. Re-detection only re-clusters unlocked
media, never renames or deletes user-edited events, and is incremental: new
media re-cluster only the time window around them.
"""

from __future__ import annotations

import json
import math
import threading
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Iterable, Optional

import numpy as np

GAP_HOURS = 4.0
TRIP_GAP_HOURS = 36.0
AWAY_KM = 40.0
TRIP_SPAN_KM = 400.0
LINK_KM = 150.0
MAX_SPEED_KMH = 300.0
VISUAL_GAP_MIN = 90.0
VISUAL_SPLIT_COS = 0.45
PEOPLE_MERGE_HOURS = 12.0
WINDOW_HOURS = 48.0
# Bump when detection rules change: the next job then runs a full detection.
EVENTS_VERSION = 2


@dataclass
class Item:
    id: int
    t: datetime
    lat: Optional[float] = None
    lon: Optional[float] = None
    vec: Optional[np.ndarray] = None
    people: frozenset = frozenset()


@dataclass
class Segment:
    items: list[Item] = field(default_factory=list)

    @property
    def start(self) -> datetime:
        return self.items[0].t

    @property
    def end(self) -> datetime:
        return self.items[-1].t

    def coords(self) -> list[tuple[float, float]]:
        return [(i.lat, i.lon) for i in self.items if i.lat is not None and i.lon is not None]

    def centre(self) -> Optional[tuple[float, float]]:
        pts = self.coords()
        if not pts:
            return None
        return float(np.median([p[0] for p in pts])), float(np.median([p[1] for p in pts]))

    def people(self) -> set:
        out: set = set()
        for item in self.items:
            out |= item.people
        return out


def km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 6371.0 * 2 * math.asin(min(1.0, math.sqrt(h)))


def home_location(items: Iterable[Item]) -> Optional[tuple[float, float]]:
    """The ~5 km grid cell you keep coming back to: most distinct months, then distinct
    days, then photos. (A long trip can out-number home in photos, never in months.)"""
    months: dict[tuple, set] = {}
    days: dict[tuple, set] = {}
    photos: Counter = Counter()
    for i in items:
        if i.lat is None or i.lon is None:
            continue
        cell = (round(i.lat * 20) / 20, round(i.lon * 20) / 20)
        months.setdefault(cell, set()).add((i.t.year, i.t.month))
        days.setdefault(cell, set()).add(i.t.date())
        photos[cell] += 1
    if not photos:
        return None
    return max(photos, key=lambda c: (len(months[c]), len(days[c]), photos[c]))


def _linked(a: Item, b: Item) -> bool:
    hours = (b.t - a.t).total_seconds() / 3600
    if hours > GAP_HOURS:
        return False
    if a.lat is not None and b.lat is not None:
        distance = km((a.lat, a.lon), (b.lat, b.lon))
        if distance > LINK_KM or distance / max(hours, 0.25) > MAX_SPEED_KMH:
            return False
    return True


def _visual_split(segment: Segment) -> list[Segment]:
    items = segment.items
    if len(items) < 4 or sum(i.vec is not None for i in items) < 4:
        return [segment]
    for k in range(2, len(items) - 1):
        if (items[k].t - items[k - 1].t).total_seconds() / 60 < VISUAL_GAP_MIN:
            continue
        before = [i.vec for i in items[:k] if i.vec is not None]
        after = [i.vec for i in items[k:] if i.vec is not None]
        if len(before) < 2 or len(after) < 2:
            continue
        a, b = np.mean(before, axis=0), np.mean(after, axis=0)
        cos = float(a @ b / max(np.linalg.norm(a) * np.linalg.norm(b), 1e-9))
        if cos < VISUAL_SPLIT_COS:
            return _visual_split(Segment(items[:k])) + _visual_split(Segment(items[k:]))
    return [segment]


def cluster(items: list[Item], home: Optional[tuple[float, float]] = None) -> list[list[int]]:
    """Group items into events; returns lists of item ids, oldest event first."""
    items = sorted(items, key=lambda i: (i.t, i.id))
    if not items:
        return []
    segments: list[Segment] = [Segment([items[0]])]
    for prev, item in zip(items, items[1:]):
        if _linked(prev, item):
            segments[-1].items.append(item)
        else:
            segments.append(Segment([item]))
    refined: list[Segment] = []
    for segment in segments:
        refined.extend(_visual_split(segment))

    home = home if home is not None else home_location(items)

    def away(segment: Segment) -> Optional[bool]:
        centre = segment.centre()
        if centre is None or home is None:
            return None
        return km(centre, home) > AWAY_KM

    merged: list[Segment] = [refined[0]]
    for segment in refined[1:]:
        last = merged[-1]
        gap_h = (segment.start - last.end).total_seconds() / 3600
        a, b = last.centre(), segment.centre()
        same_trip = (away(last) and away(segment) and a and b and km(a, b) <= TRIP_SPAN_KM and gap_h <= TRIP_GAP_HOURS)
        # Unlocated segments inside a trip (phone GPS off) stay with the trip.
        unlocated_in_trip = (b is None and away(last) and gap_h <= TRIP_GAP_HOURS and gap_h <= 18)
        pa, pb = last.people(), segment.people()
        same_people = (gap_h <= PEOPLE_MERGE_HOURS and pa and pb and len(pa & pb) / len(pa | pb) >= 0.5
                       and (a is None or b is None or km(a, b) <= AWAY_KM)
                       and last.start.date() == segment.start.date())
        if same_trip or unlocated_in_trip or same_people:
            last.items.extend(segment.items)
        else:
            merged.append(segment)
    return [[i.id for i in segment.items] for segment in merged]


# ---------------------------------------------------------------------------
# Naming
# ---------------------------------------------------------------------------

class Places:
    """Optional offline GeoNames cities file (models/geonames/cities15000.txt, CC-BY 4.0)."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._data = None

    def _load(self):
        if self._data is None:
            names, coords = [], []
            if self.path.is_file():
                with self.path.open(encoding="utf-8") as handle:
                    for line in handle:
                        parts = line.rstrip("\n").split("\t")
                        if len(parts) > 8:
                            names.append(f"{parts[1]}, {parts[8]}" if parts[8] else parts[1])
                            coords.append((float(parts[4]), float(parts[5])))
            self._data = (names, np.radians(np.array(coords, dtype=np.float64)) if coords else None)
        return self._data

    def nearest(self, lat: float, lon: float, max_km: float = 40.0) -> Optional[str]:
        names, coords = self._load()
        if coords is None:
            return None
        lat_r, lon_r = math.radians(lat), math.radians(lon)
        d = np.sin((coords[:, 0] - lat_r) / 2) ** 2 + math.cos(lat_r) * np.cos(coords[:, 0]) * np.sin((coords[:, 1] - lon_r) / 2) ** 2
        index = int(np.argmin(d))
        distance = 6371.0 * 2 * math.asin(min(1.0, math.sqrt(float(d[index]))))
        return names[index] if distance <= max_km else None


def date_range_label(start: datetime, end: datetime) -> str:
    if start.date() == end.date():
        return start.strftime("%-d %b %Y")
    if (start.year, start.month) == (end.year, end.month):
        return f"{start.day}–{end.day} {start.strftime('%b %Y')}"
    if start.year == end.year:
        return f"{start.strftime('%-d %b')} – {end.strftime('%-d %b %Y')}"
    return f"{start.strftime('%-d %b %Y')} – {end.strftime('%-d %b %Y')}"


def auto_name(start: datetime, end: datetime, centre: Optional[tuple[float, float]], people: list[str],
              places: Optional[Places], home: Optional[tuple[float, float]]) -> str:
    when = date_range_label(start, end)
    if centre is not None:
        at_home = home is not None and km(centre, home) <= AWAY_KM
        place = places.nearest(*centre) if places else None
        if at_home and people:
            return f"With {' & '.join(people[:2])} · {when}"
        if place:
            return f"{place.split(',')[0]} · {when}"
        lat, lon = centre
        return f"{abs(lat):.2f}°{'N' if lat >= 0 else 'S'} {abs(lon):.2f}°{'E' if lon >= 0 else 'W'} · {when}"
    if people:
        return f"With {' & '.join(people[:2])} · {when}"
    return when


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def _serialized(method):
    """Edits and detection never interleave (detection reads state before it writes)."""
    import functools

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._detect_lock:
            return method(self, *args, **kwargs)

    return wrapper


def _parse(ts: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(ts[:19])
    except (TypeError, ValueError):
        return None


class EventService:
    def __init__(self, services):
        self.s = services
        self.places = Places(Path(services.config.model_dir) / "geonames" / "cities15000.txt")
        # Detection reads, clusters and writes; two concurrent runs would both create events.
        self._detect_lock = threading.RLock()

    # -- loading -------------------------------------------------------------
    def _items(self, where: str = "1=1", params: tuple = ()) -> list[Item]:
        db = self.s.db
        rows = db.all(
            f"""SELECT m.id, COALESCE(m.captured_at, m.indexed_at) AS t, m.gps_lat, m.gps_lon FROM media m
                LEFT JOIN event_media em ON em.media_id = m.id
                WHERE m.deleted_at IS NULL AND COALESCE(em.locked, 0) = 0
                  -- A file time is when the file was copied, not when the moment happened:
                  -- bulk imports would otherwise become one giant fake event.
                  AND COALESCE(m.date_source, '') != 'mtime' AND ({where})""", params)
        people: dict[int, set] = {}
        if rows:
            for r in db.all(
                """SELECT f.media_id, f.person_id FROM faces f JOIN people p ON p.id=f.person_id
                   WHERE f.deleted_at IS NULL AND f.review_state != 'rejected' AND f.person_id IS NOT NULL
                     AND p.name IS NOT NULL AND TRIM(p.name) != ''"""):
                people.setdefault(r["media_id"], set()).add(r["person_id"])
        vectors = self._vectors([r["id"] for r in rows])
        items = []
        for r in rows:
            t = _parse(r["t"])
            if t is None:
                continue
            items.append(Item(r["id"], t, r["gps_lat"], r["gps_lon"], vectors.get(r["id"]), frozenset(people.get(r["id"], ()))))
        return items

    def _vectors(self, ids: list[int]) -> dict[int, np.ndarray]:
        space = self.s.visual_space()
        if space is None or not ids:
            return {}
        out = {}
        for start in range(0, len(ids), 900):
            chunk = ids[start:start + 900]
            for row in self.s.db.all(
                f"SELECT media_id, offset, sha FROM media_vectors WHERE model_key=? AND media_id IN ({','.join('?' * len(chunk))})",
                (space.key, *chunk)):
                try:
                    out[row["media_id"]] = space.file.read(row["offset"], row["sha"])
                except ValueError:
                    continue
        return out

    def _home(self, recompute: bool = False) -> Optional[tuple[float, float]]:
        """Home is computed on full runs and reused by incremental ones (stable decisions)."""
        stored = self.s.db.settings().get("events_home")
        if stored and not recompute:
            return tuple(stored)
        rows = self.s.db.all("SELECT gps_lat AS lat, gps_lon AS lon, COALESCE(captured_at, indexed_at) AS t FROM media "
                             "WHERE deleted_at IS NULL AND gps_lat IS NOT NULL")
        home = home_location(Item(0, t, r["lat"], r["lon"]) for r in rows if (t := _parse(r["t"])))
        with self.s.db.connect() as conn:
            conn.execute("INSERT INTO settings(key, value) VALUES ('events_home', ?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(list(home) if home else None),))
        return home

    # -- detection ---------------------------------------------------------------
    def detect(self, *, since_media_id: Optional[int] = None, checkpoint: Callable[[], None] = lambda: None) -> dict:
        """Full detection, or incremental around media with id > since_media_id (serialized)."""
        with self._detect_lock:
            return self._detect(since_media_id=since_media_id, checkpoint=checkpoint)

    def _detect(self, *, since_media_id: Optional[int], checkpoint: Callable[[], None]) -> dict:
        db = self.s.db
        home = self._home(recompute=since_media_id is None)
        if since_media_id is None:
            window = None
            items = self._items()
        else:
            new = db.all("SELECT COALESCE(captured_at, indexed_at) AS t FROM media WHERE id > ? AND deleted_at IS NULL",
                         (since_media_id,))
            stamps = [s for s in (_parse(r["t"]) for r in new) if s]
            if not stamps:
                return {"events_touched": 0, "items": 0, "window": None}
            lo, hi = min(stamps) - timedelta(hours=WINDOW_HOURS), max(stamps) + timedelta(hours=WINDOW_HOURS)
            # Widen to whole auto events overlapping the window so none is cut in half.
            span = db.one("""SELECT MIN(start_at) lo, MAX(end_at) hi FROM events
                             WHERE user_edited = 0 AND end_at >= ? AND start_at <= ?""", (lo.isoformat(), hi.isoformat()))
            if span and span["lo"]:
                lo, hi = min(lo, _parse(span["lo"])), max(hi, _parse(span["hi"]))
            window = (lo.isoformat(), hi.isoformat())
            items = self._items("COALESCE(m.captured_at, m.indexed_at) BETWEEN ? AND ?", window)
        checkpoint()
        groups = cluster(items, home)
        by_id = {i.id: i for i in items}
        touched = self._apply(groups, by_id, home, window)
        self.refresh_covers()
        return {"events_touched": touched, "items": len(items), "window": window, "events": len(groups)}

    def refresh_covers(self) -> None:
        """Cover = best-shot of the event (scores arrive later than detection).

        One ranked scan instead of a correlated subquery per event (5 s -> ms on 2k events)."""
        db = self.s.db
        version = db.one("SELECT MAX(formula_version) AS v FROM quality_scores")["v"]
        if version is None:
            return
        best = db.all("""SELECT event_id, media_id FROM (
                           SELECT em.event_id, em.media_id,
                                  ROW_NUMBER() OVER (PARTITION BY em.event_id ORDER BY q.score DESC, em.media_id) AS r
                           FROM event_media em JOIN quality_scores q
                             ON q.media_id = em.media_id AND q.formula_version = ?)
                         WHERE r = 1""", (version,))
        if best:
            with db.connect() as conn:
                conn.executemany("UPDATE events SET cover_media_id=? WHERE id=? AND cover_media_id IS NOT ?",
                                 [(r["media_id"], r["event_id"], r["media_id"]) for r in best])

    def _apply(self, groups: list[list[int]], by_id: dict[int, Item], home, window) -> int:
        db = self.s.db
        names = {r["id"]: r["name"] for r in db.all("SELECT id, name FROM people WHERE name IS NOT NULL")}
        scores = {r["media_id"]: r["score"] for r in db.all(
            "SELECT media_id, score FROM quality_scores WHERE formula_version=(SELECT MAX(formula_version) FROM quality_scores)")}
        touched = 0
        with db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = {r["media_id"]: r["event_id"] for r in conn.execute(
                "SELECT em.media_id, em.event_id FROM event_media em JOIN events e ON e.id = em.event_id "
                "WHERE em.locked = 0 AND e.user_edited = 0")}
            candidate_events = set(current[i] for g in groups for i in g if i in current)
            used: set[int] = set()
            for group in groups:
                # Reuse the auto event that already holds most of these items (stable ids).
                votes = Counter(current[i] for i in group if i in current and current[i] not in used)
                event_id = votes.most_common(1)[0][0] if votes else None
                members = [by_id[i] for i in group]
                start, end = members[0].t, members[-1].t
                seg = Segment(members)
                centre = seg.centre()
                people_counts = Counter(p for m in members for p in m.people)
                people = [names[p] for p, _ in people_counts.most_common(3) if p in names]
                name = auto_name(start, end, centre, people, self.places, home)
                cover = max(group, key=lambda i: (scores.get(i, -1), -i))
                values = (name, start.isoformat(), end.isoformat(), cover, centre[0] if centre else None,
                          centre[1] if centre else None, len(group), json.dumps(sorted(people_counts)))
                if event_id is None:
                    event_id = conn.execute(
                        "INSERT INTO events(name, start_at, end_at, cover_media_id, lat, lon, item_count, people) "
                        "VALUES (?,?,?,?,?,?,?,?)", values).lastrowid
                    changed = True
                else:
                    before = set(r[0] for r in conn.execute("SELECT media_id FROM event_media WHERE event_id=?", (event_id,)))
                    changed = before != set(group)
                    if changed:
                        conn.execute("UPDATE events SET name=?, start_at=?, end_at=?, cover_media_id=?, lat=?, lon=?, "
                                     "item_count=?, people=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", (*values, event_id))
                used.add(event_id)
                if changed:
                    touched += 1
                    conn.executemany("INSERT INTO event_media(media_id, event_id, locked) VALUES (?,?,0) "
                                     "ON CONFLICT(media_id) DO UPDATE SET event_id=excluded.event_id WHERE locked=0",
                                     [(i, event_id) for i in group])
            # Auto events in scope that no group reused lost all their (unlocked) items.
            if window is None:
                scope = set(r[0] for r in conn.execute("SELECT id FROM events WHERE user_edited=0"))
            else:
                scope = set(r[0] for r in conn.execute(
                    "SELECT id FROM events WHERE user_edited=0 AND end_at >= ? AND start_at <= ?", window))
            for event_id in (scope | candidate_events) - used:
                conn.execute("DELETE FROM event_media WHERE event_id=? AND locked=0", (event_id,))
                if not conn.execute("SELECT 1 FROM event_media WHERE event_id=? LIMIT 1", (event_id,)).fetchone():
                    conn.execute("DELETE FROM events WHERE id=?", (event_id,))
                    touched += 1
        return touched

    # -- edits (each returns an undo token) ----------------------------------------
    def _snapshot(self, conn, event_ids: Iterable[int]) -> dict:
        snap = {"events": [], "members": []}
        for event_id in set(event_ids):
            row = conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
            if row:
                snap["events"].append(dict(row))
                snap["members"] += [dict(r) for r in conn.execute("SELECT * FROM event_media WHERE event_id=?", (event_id,))]
        return snap

    def _log(self, conn, action: str, snapshot: dict, created: Iterable[int] = ()) -> int:
        return conn.execute("INSERT INTO event_edits(action, snapshot, created_events) VALUES (?,?,?)",
                            (action, json.dumps(snapshot), json.dumps(sorted(set(created))))).lastrowid

    def _refresh(self, conn, event_id: int) -> None:
        rows = conn.execute("""SELECT m.id, COALESCE(m.captured_at, m.indexed_at) AS t, m.gps_lat, m.gps_lon FROM event_media em
                               JOIN media m ON m.id = em.media_id WHERE em.event_id=? ORDER BY t""", (event_id,)).fetchall()
        if not rows:
            conn.execute("DELETE FROM events WHERE id=?", (event_id,))
            return
        scores = {r[0]: r[1] for r in conn.execute(
            f"SELECT media_id, score FROM quality_scores WHERE media_id IN ({','.join('?' * len(rows))})",
            tuple(r["id"] for r in rows))}
        pts = [(r["gps_lat"], r["gps_lon"]) for r in rows if r["gps_lat"] is not None]
        centre = (float(np.median([p[0] for p in pts])), float(np.median([p[1] for p in pts]))) if pts else (None, None)
        conn.execute("UPDATE events SET start_at=?, end_at=?, item_count=?, cover_media_id=?, lat=?, lon=?, "
                     "updated_at=CURRENT_TIMESTAMP WHERE id=?",
                     (rows[0]["t"], rows[-1]["t"], len(rows), max(rows, key=lambda r: scores.get(r["id"], -1))["id"],
                      *centre, event_id))

    @_serialized
    def rename(self, event_id: int, name: str) -> int:
        with self.s.db.connect() as conn:
            token = self._log(conn, "rename", self._snapshot(conn, [event_id]))
            conn.execute("UPDATE events SET name=?, user_edited=1, updated_at=CURRENT_TIMESTAMP WHERE id=?", (name, event_id))
            conn.execute("UPDATE event_media SET locked=1 WHERE event_id=?", (event_id,))
        return token

    @_serialized
    def merge(self, event_ids: list[int]) -> tuple[int, int]:
        target, *others = event_ids
        with self.s.db.connect() as conn:
            token = self._log(conn, "merge", self._snapshot(conn, event_ids))
            for other in others:
                conn.execute("UPDATE event_media SET event_id=? WHERE event_id=?", (target, other))
                conn.execute("DELETE FROM events WHERE id=?", (other,))
            conn.execute("UPDATE events SET user_edited=1 WHERE id=?", (target,))
            conn.execute("UPDATE event_media SET locked=1 WHERE event_id=?", (target,))
            self._refresh(conn, target)
        return target, token

    @_serialized
    def split(self, event_id: int, first_media_id: int) -> tuple[int, int]:
        """Everything from ``first_media_id`` (in time order) onwards becomes a new event."""
        with self.s.db.connect() as conn:
            rows = conn.execute("""SELECT em.media_id FROM event_media em JOIN media m ON m.id=em.media_id
                                   WHERE em.event_id=? ORDER BY COALESCE(m.captured_at, m.indexed_at), m.id""",
                                (event_id,)).fetchall()
            ids = [r[0] for r in rows]
            if first_media_id not in ids or ids.index(first_media_id) == 0:
                raise ValueError("Split point must be an item of the event other than its first")
            snapshot = self._snapshot(conn, [event_id])
            source = conn.execute("SELECT name FROM events WHERE id=?", (event_id,)).fetchone()
            new_id = conn.execute("INSERT INTO events(name, start_at, end_at, item_count, user_edited) VALUES (?, '', '', 0, 1)",
                                  (f"{source['name']} (2)",)).lastrowid
            token = self._log(conn, "split", snapshot, [new_id])
            moved = ids[ids.index(first_media_id):]
            conn.executemany("UPDATE event_media SET event_id=?, locked=1 WHERE media_id=?", [(new_id, m) for m in moved])
            conn.execute("UPDATE event_media SET locked=1 WHERE event_id=?", (event_id,))
            conn.execute("UPDATE events SET user_edited=1 WHERE id=?", (event_id,))
            self._refresh(conn, event_id)
            self._refresh(conn, new_id)
        return new_id, token

    @_serialized
    def move(self, media_ids: list[int], event_id: int) -> int:
        with self.s.db.connect() as conn:
            sources = [r[0] for r in conn.execute(
                f"SELECT DISTINCT event_id FROM event_media WHERE media_id IN ({','.join('?' * len(media_ids))})", tuple(media_ids))]
            snapshot = self._snapshot(conn, [*sources, event_id])
            previous = {r["media_id"]: r for r in (dict(x) for x in conn.execute(
                f"SELECT * FROM event_media WHERE media_id IN ({','.join('?' * len(media_ids))})", tuple(media_ids)))}
            snapshot["unassigned"] = [m for m in media_ids if m not in previous]
            token = self._log(conn, "move", snapshot)
            conn.executemany("INSERT INTO event_media(media_id, event_id, locked) VALUES (?,?,1) "
                             "ON CONFLICT(media_id) DO UPDATE SET event_id=excluded.event_id, locked=1",
                             [(m, event_id) for m in media_ids])
            # The user now owns these events' composition: lock every member.
            conn.execute("UPDATE event_media SET locked=1 WHERE event_id IN (%s)" % ",".join("?" * (len(sources) + 1)),
                         (*sources, event_id))
            conn.execute("UPDATE events SET user_edited=1 WHERE id IN (%s)" % ",".join("?" * (len(sources) + 1)),
                         (*sources, event_id))
            for eid in {*sources, event_id}:
                self._refresh(conn, eid)
        return token

    @_serialized
    def undo(self, token: int) -> dict:
        with self.s.db.connect() as conn:
            edit = conn.execute("SELECT * FROM event_edits WHERE id=? AND undone_at IS NULL", (token,)).fetchone()
            if edit is None:
                raise KeyError("Nothing to undo")
            snapshot = json.loads(edit["snapshot"])
            for created in json.loads(edit["created_events"]):
                conn.execute("DELETE FROM event_media WHERE event_id=?", (created,))
                conn.execute("DELETE FROM events WHERE id=?", (created,))
            for event in snapshot["events"]:
                cols = list(event)
                conn.execute(f"INSERT OR REPLACE INTO events({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                             tuple(event[c] for c in cols))
            for member in snapshot["members"]:
                conn.execute("INSERT OR REPLACE INTO event_media(media_id, event_id, locked) VALUES (?,?,?)",
                             (member["media_id"], member["event_id"], member["locked"]))
            for media_id in snapshot.get("unassigned", []):
                conn.execute("DELETE FROM event_media WHERE media_id=?", (media_id,))
            conn.execute("UPDATE event_edits SET undone_at=CURRENT_TIMESTAMP WHERE id=?", (token,))
        return {"ok": True, "action": edit["action"]}
