"""Authentication logic tests with synthetic frames and a deterministic engine, not accuracy scores."""
import base64
import io
import time

import numpy as np
import pytest
from PIL import Image

PASSWORD = 'emergency-admin-password'


def frame(color):
    out = io.BytesIO()
    Image.new('RGB', (320, 240), color).save(out, format='JPEG')
    return base64.b64encode(out.getvalue()).decode()


@pytest.fixture
def face_engine(app_services, monkeypatch):
    def detect(image):
        vector = np.array([1., 0., 0., 0.]) if image[:, :, 2].mean() > 100 else np.array([0., 1., 0., 0.])
        return [{'embedding': vector, 'bbox': [10, 10, 100, 100], 'detection': .99}]
    monkeypatch.setattr(app_services.engine, 'detect', detect)
    return app_services.engine


def enroll(client):
    response = client.post('/api/lock/face/enroll', json={'password': PASSWORD, 'images': [frame('red')] * 3})
    assert response.status_code == 200, response.text
    assert response.json()['face_enabled']


def test_face_match_recovers_session_and_rejects_other_person(client, app_services, face_engine):
    enroll(client)
    stored = app_services.db.one("SELECT value FROM settings WHERE key='admin_face_lock'")['value']
    assert frame('red') not in stored and PASSWORD not in stored
    public = client.get('/api/settings?storage=false').json()
    assert 'admin_face_lock' not in public and 'app_lock' not in public
    client.cookies.clear()
    assert client.get('/api/media').status_code == 401
    token = client.post('/api/lock/face/challenge', json={}).json()['challenge']
    assert client.post('/api/lock/face/unlock', json={'challenge': token, 'image': frame('blue')}).status_code == 401
    assert client.get('/api/media').status_code == 401
    token = client.post('/api/lock/face/challenge', json={}).json()['challenge']
    good = client.post('/api/lock/face/unlock', json={'challenge': token, 'image': frame('red')})
    assert good.status_code == 200
    assert 'httponly' in good.headers['set-cookie'].lower()
    assert 'max-age' not in good.headers['set-cookie'].lower()
    assert client.get('/api/lock').json()['unlocked']
    client.cookies.clear()
    assert client.post('/api/lock/face/unlock', json={'challenge': token, 'image': frame('red')}).status_code == 401
    assert client.post('/api/lock/unlock', json={'password': PASSWORD}).status_code == 200


def test_enrollment_requires_password_and_single_face(client, app_services, face_engine, monkeypatch):
    enroll(client)
    assert client.post('/api/lock/face/enroll', json={'password': 'wrong-password', 'images': [frame('red')] * 3}).status_code == 401
    for count in [0, 2]:
        face = {'embedding': np.ones(4), 'bbox': [0, 0, 100, 100], 'detection': .99}
        monkeypatch.setattr(face_engine, 'detect', lambda image: [face] * count)
        assert client.post('/api/lock/face/enroll', json={'password': PASSWORD, 'images': [frame('red')] * 3}).status_code == 400
    assert client.get('/api/lock').json()['face_enabled']
    assert client.request('DELETE', '/api/lock/face', json={'password': PASSWORD}).status_code == 200
    assert client.get('/api/lock').json()['enabled']
    assert not app_services.face_lock.enabled


def test_expired_model_changed_and_failed_model_stay_locked(client, app_services, face_engine, monkeypatch):
    enroll(client)
    client.cookies.clear()
    token = client.post('/api/lock/face/challenge', json={}).json()['challenge']
    app_services.face_lock._challenges[token] = time.monotonic() - 1
    assert client.post('/api/lock/face/unlock', json={'challenge': token, 'image': frame('red')}).status_code == 401
    monkeypatch.setattr(face_engine, 'status', lambda: {'model': 'different'})
    token = client.post('/api/lock/face/challenge', json={}).json()['challenge']
    assert client.post('/api/lock/face/unlock', json={'challenge': token, 'image': frame('red')}).status_code == 409
    def fail(image):
        raise RuntimeError('Model unavailable')
    monkeypatch.setattr(face_engine, 'detect', fail)
    token = client.post('/api/lock/face/challenge', json={}).json()['challenge']
    assert client.post('/api/lock/face/unlock', json={'challenge': token, 'image': frame('red')}).status_code == 503
    assert client.get('/api/media').status_code == 401
    assert client.post('/api/lock/unlock', json={'password': PASSWORD}).status_code == 200


def test_face_attempt_rate_limit_and_csrf(client, face_engine):
    enroll(client)
    client.cookies.clear()
    for _ in range(5):
        token = client.post('/api/lock/face/challenge', json={}).json()['challenge']
        assert client.post('/api/lock/face/unlock', json={'challenge': token, 'image': frame('blue')}).status_code == 401
    assert client.post('/api/lock/face/challenge', json={}).status_code == 429
    assert client.post('/api/lock/face/unlock', json={'challenge': '', 'image': frame('red')}, headers={'X-LFS-Request': ''}).status_code == 403
