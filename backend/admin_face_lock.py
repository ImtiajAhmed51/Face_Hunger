"""Local, opt-in admin face verification. Webcam matching is not spoof-resistant Face ID."""
import base64
import json
import io
from PIL import Image
import secrets
import threading
import time

import cv2
import numpy as np
from fastapi import HTTPException

# Conservative provisional cosine threshold; not calibrated on labelled admin samples.
MATCH_THRESHOLD = 0.70


class AdminFaceLock:
    def __init__(self, db, engine, lock):
        self.db, self.engine, self.lock = db, engine, lock
        self._mutex = threading.Lock()
        self._challenges = {}

    def stored(self):
        row = self.db.one("SELECT value FROM settings WHERE key='admin_face_lock'")
        try:
            value = json.loads(row['value']) if row else None
            return value if isinstance(value, dict) else None
        except (ValueError, TypeError):
            return None

    @property
    def enabled(self):
        return self.lock.enabled and self.stored() is not None

    def challenge(self):
        if not self.enabled:
            raise HTTPException(409, 'Admin face unlock is not registered')
        token = secrets.token_urlsafe(32)
        with self._mutex:
            now = time.monotonic()
            self._challenges = {k: v for k, v in self._challenges.items() if v > now}
            if len(self._challenges) >= 128:
                raise HTTPException(429, 'Too many camera attempts. Please wait.')
            self._challenges[token] = now + 60
        return token

    def vector(self, image):
        if len(image) > 2_000_000:
            raise HTTPException(413, 'Camera image too large')
        try:
            raw = base64.b64decode(image, validate=True)
            with Image.open(io.BytesIO(raw)) as header:
                if header.format != 'JPEG' or max(header.size) > 1280 or min(header.size) < 160:
                    raise ValueError('Invalid camera frame')
            frame = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
        except Exception:
            frame = None
        if frame is None or max(frame.shape[:2]) > 1280 or min(frame.shape[:2]) < 160:
            raise HTTPException(400, 'Invalid camera image; use a 640px camera capture')
        try:
            faces = self.engine.detect(frame)
        except Exception:
            raise HTTPException(503, 'Face recognition model unavailable. Use your recovery password.')
        if len(faces) != 1:
            raise HTTPException(400, 'Show exactly one face to the camera')
        face = faces[0]
        if face.get('detection', 0) < .8 or min(face['bbox'][2:]) < 80:
            raise HTTPException(400, 'Move closer and use brighter lighting')
        vector = np.asarray(face['embedding'], dtype=np.float32)
        norm = np.linalg.norm(vector)
        if vector.ndim != 1 or not np.isfinite(vector).all() or not np.isfinite(norm) or norm < 1e-6:
            raise HTTPException(400, 'Face embedding is unusable')
        return vector / norm

    def contract(self):
        status = self.engine.status()
        return {'model': status.get('model'), 'embedding': (status.get('metadata') or {}).get('embedding')}

    def enroll(self, images):
        if len(images) != 3:
            raise HTTPException(400, 'Capture three admin face samples')
        vectors = [self.vector(image) for image in images]
        if any(v.shape != vectors[0].shape or float(v @ vectors[0]) < MATCH_THRESHOLD for v in vectors[1:]):
            raise HTTPException(400, 'Samples must show the same admin. Please capture again.')
        return {'version': 1, 'contract': self.contract(), 'dimension': int(vectors[0].size),
                'threshold': MATCH_THRESHOLD, 'vectors': [v.tolist() for v in vectors]}

    def save(self, template):
        with self.db.connect() as conn:
            conn.execute("INSERT INTO settings(key,value) VALUES('admin_face_lock',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(template),))
        with self._mutex:
            self._challenges.clear()

    def verify(self, token, image):
        with self._mutex:
            expires = self._challenges.pop(token, 0)
        if expires < time.monotonic():
            raise HTTPException(401, 'Camera attempt expired. Try again.')
        stored = self.stored()
        if not self.enabled or not stored:
            raise HTTPException(401, 'Face unlock is unavailable')
        if stored.get('version') != 1 or not all(key in stored for key in ('contract', 'dimension', 'vectors', 'threshold')) or stored['threshold'] != MATCH_THRESHOLD:
            raise HTTPException(409, 'Face registration is invalid. Use recovery password.')
        vector = self.vector(image)
        if stored['contract'] != self.contract() or stored['dimension'] != vector.size:
            raise HTTPException(409, 'Recognition model changed. Use recovery password and register again.')
        try:
            templates = np.asarray(stored['vectors'], dtype=np.float32)
            if templates.shape != (3, vector.size) or not np.isfinite(templates).all() or not np.allclose(np.linalg.norm(templates, axis=1), 1., atol=1e-4):
                raise HTTPException(409, 'Face registration is invalid. Use recovery password.')
            return bool(np.min(templates @ vector) >= MATCH_THRESHOLD)
        except (ValueError, TypeError):
            raise HTTPException(409, 'Face registration is invalid. Use recovery password.')
