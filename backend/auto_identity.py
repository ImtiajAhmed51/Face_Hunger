"""Automatic identity consolidation: merge People that are the same individual, without a human.

Why simple thresholding fails on video
--------------------------------------
Face embeddings of ONE person drift with pose, light, expression and blur, so a single
centroid-vs-centroid threshold either leaves the person split (threshold too high) or fuses
different people (too low). This module combines several independent signals:

1. Crop-level evidence. Every person is represented by up to 32 diverse, good crops (not just
   a centroid). Two people are compared by their best crop pair, the mean of their top-3 crop
   pairs, AND their centroids; all three must agree. Different people rarely satisfy all three.
2. Temporal continuity inside a video. When a tracker loses a face (turn, occlusion, cut) it
   starts a new person. If two people alternate in the SAME video, a few seconds apart, at the
   SAME place on screen, that is strong extra evidence; their similarity gets a small bonus.
3. Hard "cannot-link" constraints. Two people that appear in the same frame/photo can never be
   one person; pairs the user marked "not the same" and two differently-named people are never
   merged. Constraints are enforced on the *merged group*, so chains cannot sneak past them.
4. Constrained agglomerative clustering. The most similar valid pair is merged first and the
   group's evidence is recomputed, so a person seen from the front, the side and from behind is
   joined through the intermediate views (single "bridge" crops), with a centroid guard against
   identity drift.
5. Everything is audited and undoable (one Undo per merge).

Scores are cosine measurements, not probabilities.
"""
from __future__ import annotations

import heapq
import json
import math

import numpy as np

from .clustering import ACTIVE
from .embeddings import normalize
from .engine import Engine

# (best crop pair, mean of top-3 crop pairs, centroid) that must ALL be reached.
LEVELS = {
    "conservative": (.62, .56, .50),
    "balanced": (.56, .50, .44),
    "aggressive": (.52, .46, .40),
}
MAX_CROPS = 32
MIN_QUALITY = .35
MIN_DETECTION = .65
MIN_BOX = 24
TEMPORAL_GAP = 12.0       # seconds between two people's sightings in one video
TEMPORAL_BONUS = .04


def _crop_ok(face) -> bool:
    try:
        bbox = json.loads(face["bbox"])
        if min(bbox[2:]) < MIN_BOX or float(face["detection"]) < MIN_DETECTION or float(face["quality"]) < MIN_QUALITY:
            return False
        if face["landmarks"]:
            return Engine._valid_landmarks(np.asarray(json.loads(face["landmarks"])), bbox,
                                           face["width"] or 1e9, face["height"] or 1e9)
        return True
    except (ValueError, TypeError, KeyError, IndexError):
        return False


class _Group:
    __slots__ = ("id", "members", "vecs", "meta", "centroid", "keys", "names", "faces", "version", "name")

    def __init__(self, pid, name, face_count):
        self.id, self.members, self.faces, self.version = pid, [pid], face_count, 0
        self.names = {name.strip().lower()} if name and name.strip() else set()
        self.vecs, self.meta, self.keys, self.centroid = None, [], set(), None
        self.name = name or ""


def _prune(vecs, meta):
    """Keep the best, mutually distinct crops (near-identical frames add nothing)."""
    order = sorted(range(len(meta)), key=lambda i: -meta[i]["q"])
    keep = []
    for i in order:
        if all(float(vecs[i] @ vecs[j]) < .97 for j in keep):
            keep.append(i)
        if len(keep) >= MAX_CROPS:
            break
    return vecs[keep], [meta[i] for i in keep]


def _temporal_link(a, b) -> bool:
    """Same video, a few seconds apart, same place on screen => likely a tracker split."""
    by_media = {}
    for m in a.meta:
        if m["ts"] is not None:
            by_media.setdefault(m["media"], []).append(m)
    for m in b.meta:
        if m["ts"] is None:
            continue
        for o in by_media.get(m["media"], ()):
            gap = abs(m["ts"] - o["ts"])
            size = max(m["size"], o["size"])
            dist = math.hypot(m["cx"] - o["cx"], m["cy"] - o["cy"])
            if 0.0 < gap <= TEMPORAL_GAP and dist <= .6 * size:
                return True
    return False


def _evaluate(a, b, thresholds):
    best_t, top3_t, cen_t = thresholds
    cross = a.vecs @ b.vecs.T
    flat = np.sort(cross, axis=None)[::-1]
    best, top3 = float(flat[0]), float(flat[:3].mean())
    centroid = float(a.centroid @ b.centroid)
    bonus = TEMPORAL_BONUS if _temporal_link(a, b) else 0.0
    ok = best + bonus >= best_t and top3 + bonus >= top3_t and centroid + bonus >= cen_t
    score = .5 * top3 + .3 * best + .2 * centroid + bonus
    return ok, score, dict(best_crop=round(best, 3), top3=round(top3, 3), centroid=round(centroid, 3),
                           temporal=bool(bonus))


def _impostor_floor(live, level, limit=400):
    """Learn from the library itself: two people seen in the same frame are, by definition, different
    individuals. How alike do such *known-different* pairs look? Merge thresholds are never allowed to
    sit at or below that (99th percentile + margin), so the rule adapts to this library's lighting,
    cameras and look-alike people."""
    by_key = {}
    for g in live.values():
        for key in g.keys:
            by_key.setdefault(key, []).append(g.id)
    pairs = set()
    for ids_ in by_key.values():
        if len(ids_) > 1:
            for i, a in enumerate(ids_[:6]):
                for b in ids_[i + 1:6]:
                    pairs.add((min(a, b), max(a, b)))
    pairs = sorted(pairs)
    if len(pairs) > limit:
        pick = np.random.default_rng(0).choice(len(pairs), limit, replace=False)
        pairs = [pairs[i] for i in pick]
    rows = []
    for a, b in pairs:
        ga, gb = live[a], live[b]
        cross = ga.vecs @ gb.vecs.T
        flat = np.sort(cross, axis=None)[::-1]
        rows.append((float(flat[0]), float(flat[:3].mean()), float(ga.centroid @ gb.centroid)))
    base = LEVELS[level]
    if len(rows) < 8:
        return base, len(rows)
    q = np.quantile(np.asarray(rows), .99, axis=0)
    return tuple(min(.78, max(b, float(v) + .02)) for b, v in zip(base, q)), len(rows)


def plan(db, store, level="balanced", checkpoint=None):
    """Compute (without changing anything) the merges auto-identity would perform."""
    if level not in LEVELS:
        raise ValueError(f"level must be one of {', '.join(LEVELS)}")
    stats = dict(people=0, grouped=0, merges=0, blocked_same_frame=0, blocked_user=0, blocked_names=0)
    people = db.all("SELECT id,name,face_count FROM people WHERE face_count>0 AND centroid IS NOT NULL")
    stats["people"] = len(people)
    if len(people) < 2:
        return [], stats
    groups = {p["id"]: _Group(p["id"], p["name"], p["face_count"]) for p in people}
    ids = tuple(groups)
    holders = ",".join("?" for _ in ids)

    # Crops: best per (person, media, track) so one long shot cannot dominate.
    rows = db.all(
        "SELECT * FROM (SELECT *, ROW_NUMBER() OVER (PARTITION BY person_id ORDER BY quality DESC,id) n FROM "
        "(SELECT f.*,m.width,m.height,ROW_NUMBER() OVER (PARTITION BY f.person_id,f.media_id,"
        "COALESCE(f.track_id,-f.id) ORDER BY f.quality DESC,f.detection DESC,f.id) per_track "
        "FROM faces f JOIN media m ON m.id=f.media_id "
        f"WHERE f.person_id IN ({holders}) AND {ACTIVE} AND f.quality>={MIN_QUALITY} AND f.detection>={MIN_DETECTION}) "
        f"WHERE per_track=1) WHERE n<={MAX_CROPS * 2}", ids)
    raw = {}
    for face in rows:
        if checkpoint:
            checkpoint()
        if not _crop_ok(face):
            continue
        try:
            vec = store.read(face["embedding_offset"], face["embedding_sha"])
        except (ValueError, OSError):
            continue
        x, y, w, h = json.loads(face["bbox"])
        raw.setdefault(face["person_id"], ([], []))
        raw[face["person_id"]][0].append(vec)
        raw[face["person_id"]][1].append(dict(q=float(face["quality"]), media=face["media_id"], ts=face["timestamp"],
                                              cx=x + w / 2, cy=y + h / 2, size=max(w, h)))
    for pid, (vecs, meta) in raw.items():
        g = groups[pid]
        g.vecs, g.meta = _prune(np.stack(vecs).astype(np.float32), meta)
        g.centroid = normalize(np.mean(g.vecs, axis=0))

    # Co-occurrence footprint of EVERY active face (cannot-link: one person cannot appear twice in a frame).
    for row in db.all(f"SELECT f.person_id,f.media_id,f.timestamp FROM faces f JOIN media m ON m.id=f.media_id "
                      f"WHERE f.person_id IN ({holders}) AND {ACTIVE}", ids):
        groups[row["person_id"]].keys.add((row["media_id"], None if row["timestamp"] is None else round(row["timestamp"], 3)))
    separate = {(r["person_a"], r["person_b"]) for r in db.all("SELECT person_a,person_b FROM separate_people")}
    must_not = {pid: set() for pid in ids}
    for a, b in separate:
        if a in must_not and b in must_not:
            must_not[a].add(b)
            must_not[b].add(a)

    live = {pid: g for pid, g in groups.items() if g.vecs is not None}
    stats["grouped"] = len(live)
    thresholds, negatives = _impostor_floor(live, level)
    stats["thresholds"] = [round(t, 3) for t in thresholds]       # best crop, top-3, centroid
    stats["known_different_pairs"] = negatives
    heap, counter = [], 0

    def blocked(a, b):
        if a.names and b.names and not (a.names & b.names):
            stats["blocked_names"] += 1
            return True
        if any(m in must_not.get(x, ()) for x in a.members for m in b.members):
            stats["blocked_user"] += 1
            return True
        if a.keys & b.keys:
            stats["blocked_same_frame"] += 1
            return True
        return False

    def push(a, b):
        nonlocal counter
        # Cheap pre-filter on centroids before the full crop comparison.
        if float(a.centroid @ b.centroid) < thresholds[2] - TEMPORAL_BONUS - .02:
            return
        ok, score, detail = _evaluate(a, b, thresholds)
        if ok:
            counter += 1
            heapq.heappush(heap, (-score, counter, a.id, b.id, a.version, b.version, detail))

    pool = list(live.values())
    for i, a in enumerate(pool):
        if checkpoint and i % 25 == 0:
            checkpoint()
        for b in pool[i + 1:]:
            push(a, b)

    merges = []
    while heap:
        if checkpoint:
            checkpoint()
        neg, _, aid, bid, av, bv, detail = heapq.heappop(heap)
        a, b = live.get(aid), live.get(bid)
        if a is None or b is None or a.version != av or b.version != bv:
            continue                      # stale: one side already changed
        if blocked(a, b):
            continue
        # Keep the named profile, otherwise the larger one.
        keep, drop = (a, b) if (bool(a.names), a.faces) >= (bool(b.names), b.faces) else (b, a)
        merges.append(dict(source=drop.id, target=keep.id, score=round(-neg, 3), **detail))
        vecs = np.concatenate([keep.vecs, drop.vecs])
        meta = keep.meta + drop.meta
        keep.vecs, keep.meta = _prune(vecs, meta)
        keep.centroid = normalize(np.mean(keep.vecs, axis=0))
        keep.members += drop.members
        keep.keys |= drop.keys
        keep.names |= drop.names
        keep.faces += drop.faces
        keep.version += 1
        del live[drop.id]
        for other in live.values():
            if other.id != keep.id:
                push(keep, other)
    stats["merges"] = len(merges)
    return merges, stats


def run(services, level="balanced", apply=True, checkpoint=None):
    merges, stats = plan(services.db, services.store, level, checkpoint)
    audit_ids = []
    if apply:
        for item in merges:
            if checkpoint:
                checkpoint()
            try:
                audit_ids.append(services.library.merge_people(item["source"], item["target"])["audit_id"])
            except KeyError:
                continue      # a person vanished since planning; skip safely
    return {"applied": bool(apply), "level": level, "merges": merges[:200], "merged": len(merges) if apply else 0,
            "would_merge": len(merges), "stats": stats, "audit_ids": audit_ids}
