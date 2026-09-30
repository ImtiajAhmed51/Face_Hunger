"""Face Hunger — local face search server entry point.

Serves the React frontend from frontend/dist and exposes the /api contract.
The application itself is assembled in :mod:`backend.app`.
"""

from __future__ import annotations

# OpenMP / conda + pip torch conflict (must be set before numpy/torch load)
import os

os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
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

config = Config()
config.prepare()
app = create_app(config)


def main():
    config.prepare()
    print(f"Face Hunger starting on http://{config.host}:{config.port}")
    print(f"  data_dir  = {config.data_dir}")
    print(f"  model_dir = {config.model_dir}")
    print(f"  frontend  = {config.frontend_dir}")
    uvicorn.run(
        "backend.__main__:app",
        host=config.host,
        port=config.port,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()
