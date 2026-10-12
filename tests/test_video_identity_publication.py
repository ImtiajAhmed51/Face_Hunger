"""Publication regressions use synthetic vectors and disposable databases only."""
import json
import sqlite3

import numpy as np
import pytest

from backend.embeddings import normalize
from backend.video.checkpoint import SCHEMA
from tests.helpers.fixtures import add_media
from tests.test_face_pipeline import vector


def publish_tracks(s, rows, media_id=None):
    mid = media_id or add_media(s.db, 1, kind='video')[0]
    staged = sqlite3.connect(':memory:')
    staged.row_factory = sqlite3.Row
    staged.executescript(SCHEMA)
    for tid, stamp, embedding, quality in rows:
        offset, sha = s.store.append(embedding)
        staged.execute('INSERT INTO detections(bbox,timestamp,detection,embedding_offset,embedding_sha,jpeg,quality,track_id) '
                       'VALUES (?,?,?,?,?,?,?,?)',
                       (json.dumps([10, 10, 40, 40]), stamp, .95, offset, sha, b'test-jpeg', quality, tid))
    staged.commit()
    s.worker._publish(mid, staged, {'width':100,'height':100,'duration':120,'meta_version':1}, b'test-jpeg', 0, s.db.settings())
    staged.close()
    return s.db.all('SELECT * FROM faces WHERE media_id=? AND deleted_at IS NULL ORDER BY timestamp,id', (mid,))


@pytest.mark.parametrize('different_person', [False, True])
def test_later_segment_uses_all_accepted_track_evidence(app_services, different_person):
    first = vector()
    side = normalize(.6*vector() + .8*vector(1))
    later = vector(2) if different_person else normalize(.1*vector() + .995*vector(1))
    rows = [(1, 0., first, .95)] + [(1, float(i*3), side, .8) for i in range(1, 6)]
    rows += [(2, 40., later, .9), (2, 43., later, .8)]
    faces = publish_tracks(app_services, rows)
    assert all(f['person_id'] is not None for f in faces)
    assert len({f['person_id'] for f in faces}) == (2 if different_person else 1)


def test_track_propagation_cannot_assign_same_person_twice_in_frame(app_services):
    rows = [(1, 0., vector(), .95), (1, 3., vector(), .8),
            (2, 10., vector(), .95), (2, 3., vector(), .8)]
    faces = publish_tracks(app_services, rows)
    same_frame = [f['person_id'] for f in faces if f['timestamp'] == 3 and f['person_id'] is not None]
    assert len(same_frame) == len(set(same_frame))


def test_republication_preserves_manual_decisions_and_member_rejection(app_services):
    s = app_services
    rows = [(1, 0., vector(), .95), (1, 3., vector(), .8), (1, 6., vector(), .7)]
    first = publish_tracks(s, rows)
    pid, mid = first[0]['person_id'], first[0]['media_id']
    with s.db.connect() as c:
        other = c.execute("INSERT INTO people(name) VALUES ('User choice')").lastrowid
        c.execute("UPDATE faces SET person_id=?,manual=1,review_state='confirmed' WHERE id=?", (other, first[2]['id']))
        c.execute('INSERT INTO rejections(face_id,person_id) VALUES (?,?)', (first[1]['id'], pid))
        c.execute("UPDATE faces SET person_id=NULL,review_state='unreviewed' WHERE id=?", (first[1]['id'],))
    s.cluster.invalidate()
    second = publish_tracks(s, rows, media_id=mid)
    assert [f['id'] for f in first] == [f['id'] for f in second]
    assert second[1]['person_id'] is None
    assert second[2]['person_id'] == other and second[2]['manual'] == 1
    assert s.db.one('SELECT COUNT(*) n FROM rejections')['n'] == 1
