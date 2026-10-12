"""Duplicate-person suggestions: "are these two People the same individual?"

Why the old version showed nothing
----------------------------------
It kept only ONE crop per media item and then demanded >=2 crops per person, each
with quality >= .70, a >=40 px box and a cross-person cosine >= .70 for 75% of crops.
A person who appears in a single video (the common case for video libraries) can never
satisfy that, and profile / closed-eye frames never reach quality .70, so every pair
was filtered out before it was compared.

This version
------------
* one best crop per *track* (a continuous appearance in a video) instead of per media,
  so a person that lives in one video still yields several independent crops;
* realistic crop gates (quality >= .40, detection >= .70, box >= 24 px);
* compares people with several complementary measures (centroid, best crop pair,
  mean of the top-3 crop pairs) instead of a single 0.70 gate;
* ranks suggestions as high / medium / low confidence; a human still confirms the merge;
* never suggests pairs that co-occur in the same frame (cannot be one person) or that
  the user already marked "not the same";
* reports *why* candidates were dropped, so an empty list is explainable.

Scores are cosine measurements, never probabilities of identity.
"""
import json
import math

import numpy as np

from .clustering import ACTIVE
from .embeddings import normalize
from .engine import Engine

MIN_QUALITY = .40
MIN_DETECTION = .70
MIN_BOX = 24
CROPS_PER_PERSON = 12
MAX_PEOPLE = 600
MAX_RESULTS = 60


def usable(face):
    try:
        q, det = float(face['quality']), float(face['detection'])
        bbox = json.loads(face['bbox'])
        if not (math.isfinite(q) and math.isfinite(det) and q >= MIN_QUALITY and det >= MIN_DETECTION
                and len(bbox) == 4 and min(bbox[2:]) >= MIN_BOX):
            return False
        raw = face['landmarks'] if 'landmarks' in face.keys() else None
        if raw:  # legacy rows without landmarks are still allowed
            landmarks = np.asarray(json.loads(raw))
            return Engine._valid_landmarks(landmarks, bbox, face['width'] or 1e9, face['height'] or 1e9)
        return True
    except (ValueError, TypeError, KeyError):
        return False


def _confidence(centroid, best, top3):
    if best >= .62 and centroid >= .52 and top3 >= .55:
        return 'high'
    if best >= .54 and (centroid >= .46 or top3 >= .48):
        return 'medium'
    return 'low'


def suggestions(db, store, cluster, matching_threshold, stats=None):
    stats = stats if stats is not None else {}
    stats.update(people=0, people_with_crops=0, pairs_compared=0, below_threshold=0,
                 same_frame=0, marked_separate=0, no_usable_crops=0)
    people = db.all('SELECT id,name,centroid,face_count,representative_face_id FROM people '
                    f'WHERE face_count>0 AND centroid IS NOT NULL ORDER BY face_count DESC LIMIT {MAX_PEOPLE}')
    stats['people'] = len(people)
    if len(people) < 2:
        return []
    by_id = {p['id']: p for p in people}
    placeholders = ','.join('?' for _ in by_id)
    # Best crop per (person, media, track): a person in ONE video still contributes several
    # independent appearances, while 100 frames of the same shot cannot inflate the evidence.
    rows = db.all(
        'SELECT * FROM (SELECT *, ROW_NUMBER() OVER (PARTITION BY person_id ORDER BY quality DESC,id) n '
        'FROM (SELECT f.*,m.width,m.height,ROW_NUMBER() OVER '
        '(PARTITION BY f.person_id,f.media_id,COALESCE(f.track_id,-f.id) '
        'ORDER BY f.quality DESC,f.detection DESC,f.id) per_track '
        'FROM faces f JOIN media m ON m.id=f.media_id '
        f'WHERE f.person_id IN ({placeholders}) AND {ACTIVE} '
        f'AND f.quality>={MIN_QUALITY} AND f.detection>={MIN_DETECTION}) WHERE per_track=1) '
        f'WHERE n<={CROPS_PER_PERSON}', tuple(by_id))
    samples = {}
    for face in rows:
        if not usable(face):
            continue
        try:
            v = store.read(face['embedding_offset'], face['embedding_sha'])
        except (ValueError, OSError):
            continue
        samples.setdefault(face['person_id'], []).append((face, v))
    ids, vectors = [], []
    for pid in by_id:
        crops = samples.get(pid)
        if not crops:
            stats['no_usable_crops'] += 1
            continue
        try:
            vectors.append(normalize(np.mean([v for _, v in crops], axis=0)))
            ids.append(pid)
        except ValueError:
            continue  # One inconsistent identity must not hide other suggestions.
    stats['people_with_crops'] = len(ids)
    if len(ids) < 2:
        return []
    centers = np.stack(vectors)
    similarities = centers @ centers.T
    base = float(matching_threshold)
    centroid_floor = max(.40, base - .08)   # lenient: a human confirms every merge
    crop_floor = max(.50, base + .02)
    candidates = []
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            stats['pairs_compared'] += 1
            centroid = float(similarities[i, j])
            cross = np.stack([v for _, v in samples[ids[i]]]) @ np.stack([v for _, v in samples[ids[j]]]).T
            flat = np.sort(cross, axis=None)[::-1]
            best, top3 = float(flat[0]), float(flat[:3].mean())
            if centroid < centroid_floor and best < crop_floor:
                stats['below_threshold'] += 1
                continue
            candidates.append((.6 * best + .4 * centroid, centroid, best, top3, i, j, cross))
    candidates.sort(key=lambda item: item[0], reverse=True)
    result = []
    with db.connect() as conn:
        separated = {tuple(r) for r in conn.execute('SELECT person_a,person_b FROM separate_people')}
        for score, centroid, best, top3, i, j, cross in candidates[:600]:
            a, b = ids[i], ids[j]
            if (min(a, b), max(a, b)) in separated:
                stats['marked_separate'] += 1
                continue
            if cluster._collision(conn, a, b):
                stats['same_frame'] += 1
                continue
            row, col = np.unravel_index(int(np.argmax(cross)), cross.shape)
            pair_a, pair_b = dict(by_id[a]), dict(by_id[b])
            # Show the two crops that actually look most alike.
            pair_a['representative_face_id'] = samples[a][int(row)][0]['id']
            pair_b['representative_face_id'] = samples[b][int(col)][0]['id']
            result.append(dict(a=pair_a, b=pair_b, similarity=score,
                               confidence=_confidence(centroid, best, top3),
                               scores=dict(centroid=round(centroid, 3), best_crop=round(best, 3),
                                           top3=round(top3, 3)),
                               evidence=dict(a_media=len(samples[a]), b_media=len(samples[b]))))
            if len(result) >= MAX_RESULTS:
                break
    return result
