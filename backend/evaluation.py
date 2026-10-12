"""Offline evaluation against a labeled validation set of faces.

Build a small labeled set (confirmed people + their faces) and measure
precision / recall / F-beta of the current matching_threshold. Call after
any threshold or model change so accuracy claims are objective, not subjective.

Usage (API or CLI-style):
    from backend.evaluation import evaluate
    report = evaluate(db, store, cluster)
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from .embeddings import normalize

MIN_LABELED_PEOPLE = 5
MIN_LABELED_FACES = 20


def _labeled_pairs(db) -> List[Tuple[int, int, np.ndarray]]:
    """Return (face_id, person_id, embedding) for confirmed / named faces."""

    rows = db.all(
        """
        SELECT f.id, f.person_id, f.embedding_offset, f.embedding_sha
        FROM faces f
        JOIN media m ON m.id = f.media_id
        JOIN people p ON p.id = f.person_id
        WHERE f.deleted_at IS NULL AND m.deleted_at IS NULL AND m.missing = 0
          AND f.review_state = 'confirmed' AND f.manual = 1
          AND TRIM(COALESCE(p.name, '')) != ''
        ORDER BY f.id
        """
    )
    return rows


def evaluate(db, store, matching_threshold: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """Leave-one-out style evaluation on confirmed+named faces.

    For each labeled face, compute cosine similarity to every *other* person's
    centroid (built from remaining labeled faces). A true positive is matching
    the correct person above threshold; a false positive is matching a wrong
    person above threshold.
    """
    settings = db.settings()
    threshold = float(
        matching_threshold
        if matching_threshold is not None
        else settings.get("matching_threshold", 0.48)
    )

    rows = _labeled_pairs(db)
    if len(rows) < MIN_LABELED_FACES:
        return None

    # Group embeddings by person
    by_person: Dict[int, List[Tuple[int, np.ndarray]]] = {}
    for r in rows:
        vec = store.read(r["embedding_offset"], r["embedding_sha"])
        by_person.setdefault(r["person_id"], []).append((r["id"], vec))

    if len(by_person) < MIN_LABELED_PEOPLE:
        return None

    # Build leave-one-out centroids
    tp = fp = fn = 0
    scores_correct: List[float] = []
    scores_wrong: List[float] = []

    for pid, faces in by_person.items():
        others = {k: v for k, v in by_person.items() if k != pid}
        if not others:
            continue
        if len(faces) < 2:
            continue
        own_sum = np.sum([v for _, v in faces], axis=0, dtype=np.float64)
        other_cents = {
            opid: normalize(np.mean([v for _, v in ofs], axis=0))
            for opid, ofs in others.items()
        }
        for fid, vec in faces:
            # Similarity to own person
            try:
                own_cent = normalize(own_sum - vec)
            except ValueError:
                continue
            sim_own = float(vec @ own_cent)
            scores_correct.append(sim_own)
            # Best wrong match
            best_wrong = max((float(vec @ c) for c in other_cents.values()), default=0.0)
            scores_wrong.append(best_wrong)
            matched_own = sim_own >= threshold
            matched_wrong = best_wrong >= threshold
            if matched_own and not matched_wrong:
                tp += 1
            else:
                fn += 1
                if matched_wrong:
                    fp += 1


    precision = tp / (tp + fp) if (tp + fp) else 1.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    beta = 0.5
    b2 = beta * beta
    f_beta = (
        0.0
        if (precision + recall) == 0
        else (1 + b2) * precision * recall / (b2 * precision + recall)
    )

    return {
        "threshold": threshold,
        "n_faces": len(scores_correct),
        "n_people": len(by_person),
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f_beta": round(f_beta, 4),
        "false_match_rate": round(fp / len(scores_correct), 4) if scores_correct else None,
        "missed_match_rate": round(fn / len(scores_correct), 4) if scores_correct else None,
        "mean_correct_sim": round(float(np.mean(scores_correct)), 4) if scores_correct else None,
        "mean_wrong_sim": round(float(np.mean(scores_wrong)), 4) if scores_wrong else None,
        "note": "Leave-one-out on manually confirmed named faces only. Expand the labeled set for reliable numbers.",
    }
