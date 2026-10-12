"""Face Hunger — local face search server entry point.

Serves the React frontend from frontend/dist and exposes the /api contract.
The application itself is assembled in :mod:`backend.app`.
"""

from __future__ import annotations

# Bound native worker pools before numpy/torch load. Conflicting optional
# FAISS runtimes are handled by faiss_runtime, without bypassing OpenMP checks.
import os

os.environ.setdefault("KMP_INIT_AT_FORK", "FALSE")
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("VECLIB_MAXIMUM_THREADS", "1")
os.environ.setdefault("NUMEXPR_NUM_THREADS", "1")
os.environ.setdefault("XFORMERS_DISABLED", "1")
os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")

import uvicorn

from .app import create_app
from .config import Config
from .ops import logging as structured_logging

config = Config()
config.prepare()
structured_logging.configure(config.log_level, config.log_format)
app = create_app(config)


def main():
    config.prepare()
    print(f"Face Hunger starting on http://{config.host}:{config.port}")
    print(f"  data_dir  = {config.data_dir}")
    print(f"  model_dir = {config.model_dir}")
    print(f"  frontend  = {config.frontend_dir}")
    for warning in app.state.services.config_warnings:
        print(f"  warning: {warning}")
    uvicorn.run(
        app,  # pass the existing instance: python -m must not import a second service container
        host=config.host,
        port=config.port,
        reload=False,
        timeout_graceful_shutdown=5,  # open progress streams must not block restart forever
        log_level=config.log_level.lower(),
        access_log=False,  # replaced by the request-id access log
        log_config=None,  # keep our structured handlers
    )


if __name__ == "__main__":
    main()
