"""Supervised matching tests use disposable, explicitly labelled data."""
import json

import numpy as np

from backend.match_learning import train, status, load, probability, MODEL_KEY
from tests.helpers.fixtures import add_media
from tests.test_face_pipeline import insert_face, vector


def labelled(s, n=200, same_media=False):
    media = add_media(s.db, 1 if same_media else n)
    with s.db.connect() as c:
        pid = c.execute('INSERT INTO people DEFAULT VALUES').lastrowid
    for i in range(n):
        correct = int(i % 2 == 0)
        fid = insert_face(s.db, s.store, vector(), pid, media[0] if same_media else media[i])
        score = .92 if correct else .55
        with s.db.connect() as c:
            c.execute('UPDATE faces SET manual=1,review_state=?,quality=.8,similarity=? WHERE id=?',
                      ('confirmed' if correct else 'rejected', score, fid))
            c.execute('INSERT INTO face_match_feedback(face_id,person_id,similarity,correct) VALUES (?,?,?,?)',
                      (fid, pid, score, correct))


def test_insufficient_reviews_does_not_activate(client, app_services):
    result = client.post('/api/review/training')
    assert result.status_code == 200 and not result.json()['activated']
    assert client.get('/api/review/training').json()['samples'] == 0
    assert not app_services.db.one('SELECT * FROM settings WHERE key=?', (MODEL_KEY,))
    assert client.post('/api/review/training', headers={'X-LFS-Request': ''}).status_code == 403


def test_model_trains_persists_invalidates_and_resets(client, app_services):
    s = app_services
    labelled(s)
    report = client.post('/api/review/training').json()
    assert report['activated'] and report['validation']['false_matches'] == 0
    assert report['train_samples'] + report['validation']['samples'] == 200
    assert status(s.db)['active']
    with s.db.connect() as c:
        model = load(c, s.db.path)
    assert probability(model, .92, .8, .9) > .90
    assert probability(model, .55, .8, .9) < .90
    # Source contract change disables the old classifier without discarding it.
    (s.db.path.parent / 'face-model.json').write_text(json.dumps({'model': 'changed'}))
    assert not status(s.db)['active']
    (s.db.path.parent / 'face-model.json').unlink()
    assert status(s.db)['active']
    with s.db.connect() as c:
        c.execute('DELETE FROM face_match_feedback WHERE face_id=1')
    assert not status(s.db)['active']
    assert client.delete('/api/review/training').status_code == 200
    assert s.db.one('SELECT COUNT(*) n FROM face_match_feedback')['n'] == 199
    assert 'local_match_model' not in s.db.settings()


def test_repeated_video_labels_cannot_validate(app_services):
    labelled(app_services, same_media=True)
    assert not train(app_services.db)['activated']


def test_automatic_confirmations_not_training_labels(app_services):
    labelled(app_services)
    with app_services.db.connect() as c:
        c.execute('UPDATE faces SET manual=0')
    assert status(app_services.db)['samples'] == 0
    assert not train(app_services.db)['activated']


def test_failed_validation_keeps_previous_model(app_services):
    s = app_services
    labelled(s)
    assert train(s.db)['activated']
    before = s.db.one('SELECT value FROM settings WHERE key=?', (MODEL_KEY,))['value']
    with s.db.connect() as c:
        c.execute('UPDATE face_match_feedback SET similarity=.6')
    report = train(s.db)
    assert not report['activated']
    assert s.db.one('SELECT value FROM settings WHERE key=?', (MODEL_KEY,))['value'] == before
    assert not status(s.db)['active']


def test_trained_classifier_filters_new_ambiguous_score(app_services):
    s = app_services
    labelled(s)
    assert train(s.db)['activated']
    media = add_media(s.db, 1)[0]
    # .60 is above cosine floor but below learned positive evidence.
    query = vector() * .6 + vector(1) * .8
    fid = insert_face(s.db, s.store, query, None, media)
    with s.db.connect() as c:
        c.execute('UPDATE faces SET quality=.8 WHERE id=?', (fid,))
        pid, score = s.cluster.assign(c, query, face_id=fid)
        assert pid is None and np.isclose(score, .6)


def test_nonfinite_query_features_cannot_be_accepted():
    model = {'coefficients': [20., 0., 0.], 'intercept': 0.}
    assert probability(model, .9, float('nan'), .9) == 0
    assert probability(model, float('inf'), .8, .9) == 0
