"""Check normal startup and open-SSE shutdown on disposable data.

Run from the project virtual environment: python scripts/check_startup.py.
Never uses the real data directory, models or registered media folders.
"""

import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

with tempfile.TemporaryDirectory(prefix="fh-startup-") as directory:
    root = Path(directory)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = {
        **os.environ,
        "LFS_DATA_DIR": str(root / "data"),
        "LFS_MODEL_DIR": str(root / "models"),
        "LFS_ALLOWED_ROOTS": str(root),
        "LFS_WATCH": "false",
        "LFS_APP_PASSWORD": "",
        "LFS_HOST": "127.0.0.1",
        "LFS_PORT": str(port),
        "LFS_LOG_LEVEL": "INFO",
        "NO_ALBUMENTATIONS_UPDATE": "1",
    }
    with (root / "server.log").open("w") as log:
        process = subprocess.Popen(
            [sys.executable, "-m", "backend"], cwd=Path(__file__).resolve().parents[1], env=env, stdout=log, stderr=log
        )
        stream = None
        try:
            for attempt in range(100):
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1) as response:
                        assert json.load(response)["status"] == "ok"
                    break
                except OSError:
                    time.sleep(0.1)
            else:
                raise AssertionError("Server failed to start")
            stream = urllib.request.urlopen(f"http://127.0.0.1:{port}/api/jobs/events", timeout=10)
            started = time.monotonic()
            process.terminate()
            process.wait(timeout=15)
            print(
                "Normal startup HTTP 200; open-SSE shutdown seconds",
                round(time.monotonic() - started, 2),
                "exit",
                process.returncode,
            )
            assert process.returncode in (0, -signal.SIGTERM)
            assert "Application shutdown complete." in (root / "server.log").read_text()
        finally:
            if stream:
                stream.close()
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=15)
