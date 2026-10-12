"""Local supervised candidate classifier. ArcFace vectors are never changed.

Only explicit, scored human reviews are labels. Media-grouped holdout prevents
repeated video frames from leaking into both training and validation.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
import json
import math
import threading
from pathlib import Path

import numpy as np

MODEL_KEY = 'local_match_model'
REPORT_KEY = 'local_match_training'
MIN_SAMPLES = 100
PROBABILITY_FLOOR = .90
_lock = threading.Lock()


def samples(conn):
    return [dict(r) for r in conn.execute(
        "SELECT v.face_id,v.person_id,v.similarity,v.correct,f.media_id,"
        "COALESCE(f.quality,.5) quality,f.detection FROM face_match_feedback v "
        "JOIN faces f ON f.id=v.face_id JOIN media m ON m.id=f.media_id "
        "WHERE f.manual=1 AND f.person_id=v.person_id AND f.deleted_at IS NULL "
        "AND m.deleted_at IS NULL AND m.missing=0 "
        "AND ((v.correct=1 AND f.review_state='confirmed') "
        "OR (v.correct=0 AND f.review_state='rejected')) "
        "ORDER BY v.reviewed_at DESC,v.face_id DESC LIMIT 4000")
        if all(math.isfinite(float(r[k])) for k in ('similarity', 'quality', 'detection'))
        and -1 <= r['similarity'] <= 1 and 0 <= r['quality'] <= 1 and 0 <= r['detection'] <= 1]


def fingerprint(rows):
    return hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()


def contract(db_path):
    path = Path(db_path).parent / 'face-model.json'
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else 'legacy-unverified'


def load(conn, db_path):
    row = conn.execute('SELECT value FROM settings WHERE key=?', (MODEL_KEY,)).fetchone()
    if not row:
        return None
    try:
        model = json.loads(row[0])
        if (model['version'] != 1 or model['contract'] != contract(db_path)
                or model['feedback'] != fingerprint(samples(conn))):
            return None
        values = model['coefficients'] + [model['intercept']]
        if len(model['coefficients']) != 3 or not all(math.isfinite(float(x)) for x in values):
            return None
        model['coefficients'] = [float(x) for x in model['coefficients']]
        model['intercept'] = float(model['intercept'])
        return model
    except (ValueError, KeyError, TypeError, OSError):
        return None


def probability(model, similarity, quality=.5, detection=.9):
    features = [similarity, .5 if quality is None else quality, .9 if detection is None else detection]
    if not all(math.isfinite(float(x)) for x in features) or not (-1 <= features[0] <= 1
            and 0 <= features[1] <= 1 and 0 <= features[2] <= 1):
        return 0.0
    z = float(np.dot(model['coefficients'], features)) + model['intercept']
    return 1 / (1 + math.exp(-max(-60, min(60, z))))


def metrics(y, predicted):
    tp = int(np.sum((y == 1) & predicted)); fp = int(np.sum((y == 0) & predicted))
    fn = int(np.sum((y == 1) & ~predicted))
    precision = tp / (tp + fp) if tp + fp else 0.
    recall = tp / (tp + fn) if tp + fn else 0.
    return dict(precision=precision, recall=recall, true_matches=tp, false_matches=fp,
                missed_matches=fn, samples=len(y))


def train(db):
    # No queue of competing fits. This small bounded fit runs off the UI thread.
    if not _lock.acquire(blocking=False):
        return {'activated': False, 'reason': 'Training is already running.'}
    try:
        with db.connect() as conn:
            rows = samples(conn)
            settings = conn.execute("SELECT value FROM settings WHERE key='matching_threshold'").fetchone()
        threshold = float(json.loads(settings[0])) if settings else .48
        stamp = fingerprint(rows); model_contract = contract(db.path)
        positive = sum(r['correct'] == 1 for r in rows)
        report = dict(activated=False, trained_at=datetime.now(timezone.utc).isoformat(), samples=len(rows), positive_samples=positive,
                      negative_samples=len(rows)-positive, algorithm='logistic_regression',
                      reason='Need 100 scored human reviews, including 30 matches and 30 non-matches.')
        if len(rows) >= MIN_SAMPLES and min(positive, len(rows)-positive) >= 30:
            from sklearn.linear_model import LogisticRegression
            from sklearn.model_selection import GroupShuffleSplit
            x = np.asarray([[r['similarity'], r['quality'], r['detection']] for r in rows])
            y = np.asarray([r['correct'] for r in rows])
            groups = np.asarray([r['media_id'] for r in rows])
            report['reason'] = 'Need independent media groups with both labels in training and holdout.'
            if len(set(groups)) >= 8:
                for training, holdout in GroupShuffleSplit(n_splits=20, test_size=.3, random_state=42).split(x, y, groups):
                    if all(np.sum(y[indices] == label) >= minimum
                           for indices, minimum in ((training, 20), (holdout, 10)) for label in (0, 1)):
                        break
                else:
                    training = holdout = None
                if training is not None:
                    fit = LogisticRegression(C=10., max_iter=300, random_state=42).fit(x[training], y[training])
                    predicted = (fit.predict_proba(x[holdout])[:, 1] >= PROBABILITY_FLOOR) & (x[holdout, 0] >= threshold)
                    measured = metrics(y[holdout], predicted)
                    baseline = metrics(y[holdout], x[holdout, 0] >= threshold)
                    report.update(validation=measured, baseline=baseline, train_samples=len(training),
                                  holdout_media=len(set(groups[holdout])), probability_floor=PROBABILITY_FLOOR)
                    # Precision-first gate; never present training accuracy as validation.
                    report['activated'] = (measured['precision'] >= .95 and measured['recall'] >= .5
                                           and measured['true_matches'] >= 10
                                           and measured['precision'] >= baseline['precision'])
                    report['reason'] = ('Passed media-grouped holdout; active as a conservative match filter.'
                                        if report['activated'] else 'Validation insufficient; cosine matching remains in use.')
                    model = dict(version=1, coefficients=fit.coef_[0].tolist(), intercept=float(fit.intercept_[0]),
                                 feedback=stamp, contract=model_contract)
        with db.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            if stamp != fingerprint(samples(conn)) or model_contract != contract(db.path):
                report.update(activated=False, reason='Reviews or model changed during training; train again.')
            if report['activated']:
                conn.execute('INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                             (MODEL_KEY, json.dumps(model)))
            conn.execute('INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                         (REPORT_KEY, json.dumps(report)))
        return report
    finally:
        _lock.release()


def status(db):
    with db.connect() as conn:
        rows = samples(conn)
        model = load(conn, db.path)
        row = conn.execute('SELECT value FROM settings WHERE key=?', (REPORT_KEY,)).fetchone()
    positive = sum(r['correct'] == 1 for r in rows)
    previous = json.loads(row[0]) if row else None
    note = ("Learned matching is active." if model is not None else
            "Reviews or the model changed; train again before learned matching is used."
            if previous and previous.get('activated') else
            previous.get('reason') if previous else "No trained model yet; cosine matching remains in use.")
    return dict(active=model is not None, samples=len(rows), positive_samples=positive,
                negative_samples=len(rows)-positive, minimum_samples=MIN_SAMPLES,
                state_note=note, last_training=previous)
