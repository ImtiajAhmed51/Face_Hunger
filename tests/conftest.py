import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("NO_ALBUMENTATIONS_UPDATE", "1")


class FakeEngine:
    """Engine stand-in: no model files, never detects faces."""

    detection_size = 640
    multi_scale = False

    def status(self):
        return {"state": "ready", "provider": "CPUExecutionProvider", "provider_label": "CPU",
                "available_providers": ["CPUExecutionProvider"], "model": "fake", "error": None,
                "multi_scale": False, "detection_size": 640}

    def configure(self, detection_size=None, multi_scale=None):
        pass

    def load(self):
        return self.status()

    def detect(self, image):
        from backend.engine import Detections
        return Detections()


@pytest.fixture
def make_config(tmp_path):
    from backend.config import Config

    def _make(**overrides):
        frontend = tmp_path / "frontend"
        frontend.mkdir(exist_ok=True)
        (frontend / "index.html").write_text("<!doctype html><title>t</title>")
        (frontend / "assets").mkdir(exist_ok=True)
        values = dict(data_dir=tmp_path / "data", model_dir=tmp_path / "models",
                      frontend_dir=frontend, allowed_roots=str(tmp_path))
        values.update(overrides)
        return Config(**values)
    return _make


@pytest.fixture
def app_services(make_config):
    from backend.services.container import Services
    services = Services(make_config(), engine=FakeEngine())
    yield services
    services.close()


@pytest.fixture
def client(app_services):
    from fastapi.testclient import TestClient

    from backend.app import create_app
    app = create_app(services=app_services)
    with TestClient(app) as c:
        c.headers.update({"X-LFS-Request": "1"})
        yield c
