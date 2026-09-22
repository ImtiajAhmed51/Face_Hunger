"""Auto-tune the global matching_threshold from accumulated review feedback.

Every reviewed face (review_state 'confirmed'/'rejected') with a stored
`similarity` is a labeled example: confirmed => the match at that
similarity was correct, rejected => it was wrong. This sweeps that
history to find the threshold that maximizes F-beta (precision-weighted,
since a wrong auto-merge is worse than one extra item in the review
queue), then nudges the live matching_threshold toward it with an EMA
so a handful of reviews can't swing it wildly.

Drop this file in backend/ alongside clustering.py.
"""

from __future__ import annotations

import json
from typing import List, Optional, Tuple

MIN_SAMPLES = 40   # need at least this many labeled faces before trusting a fit
EMA_ALPHA = 0.25    # how fast the live threshold moves toward the new estimate
LO, HI = 0.3, 0.8   # matches Settings.matching_threshold bounds (db.py / clustering.py)
F_BETA = 0.5        # <1 => weight precision over recall


def _f_beta(precision: float, recall: float, beta: float = F_BETA) -> float:
    if precision == 0 and recall == 0:
        return 0.0
    b2 = beta * beta
    denom = b2 * precision + recall
    return 0.0 if denom == 0 else (1 + b2) * precision * recall / denom


def _sweep(pairs: List[Tuple[float, bool]]) -> Tuple[float, float, float, float]:
    """pairs: (similarity, was_correct). Returns (threshold, f_beta, precision, recall)."""
    candidates = sorted({round(s, 6) for s, _ in pairs} | {LO, HI})
    total_pos = sum(1 for _, ok in pairs if ok)
    best = (LO, 0.0, 0.0, 0.0)
    for t in candidates:
        if not (LO <= t <= HI):
            continue
        tp = sum(1 for s, ok in pairs if s >= t and ok)
        fp = sum(1 for s, ok in pairs if s >= t and not ok)
        fn = total_pos - tp
        precision = tp / (tp + fp) if (tp + fp) else 1.0
        recall = tp / (tp + fn) if (tp + fn) else 0.0
        score = _f_beta(precision, recall)
        if score > best[1]:
            best = (t, score, precision, recall)
    return best


def autotune(db, cluster=None) -> Optional[dict]:
    """Recompute matching_threshold from review history and persist it.

    `db` is the existing Database instance (backend/db.py). `cluster` is
    accepted for symmetry with other mutation endpoints but isn't required
    here: Clustering._threshold() re-reads settings on every call, so no
    cache invalidation is needed just because the threshold moved.

    Returns a report dict, or None if there isn't enough labeled data yet.
    Cheap enough to call after every review_face() commit for typical
    library sizes (a few thousand reviewed faces); move it to a periodic
    job if that stops being true.
    """
    # Cap sample size so autotune stays O(1) as the library grows past tens of
    # thousands of reviews. Recent rows are most relevant for threshold drift.
    rows = db.all(
        "SELECT similarity, review_state FROM faces "
        "WHERE similarity IS NOT NULL AND review_state IN ('confirmed','rejected') "
        "AND deleted_at IS NULL "
        "ORDER BY id DESC LIMIT 4000"
    )
    if len(rows) < MIN_SAMPLES:
        return None

    pairs = [(r["similarity"], r["review_state"] == "confirmed") for r in rows]
    suggested, f_beta, precision, recall = _sweep(pairs)

    with db.connect() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key='matching_threshold'").fetchone()
        current = json.loads(row[0]) if row else 0.48
        tuned = current + EMA_ALPHA * (suggested - current)
        tuned = round(max(LO, min(HI, tuned)), 3)
        conn.execute("UPDATE settings SET value=? WHERE key='matching_threshold'", (json.dumps(tuned),))

    return {
        "threshold": tuned,
        "suggested": suggested,
        "f_beta": round(f_beta, 4),
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "n_samples": len(rows),
    }
