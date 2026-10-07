# syntax=docker/dockerfile:1.7
# Reproducible image: pinned base tags (pin digests at release, see docs/RELEASE.md), pinned
# Python packages (requirements.lock), `npm ci` from the lock file, fixed timestamps.
# No model is downloaded at build time: mount your model folder at /models.
#
#   docker build --build-arg SOURCE_DATE_EPOCH=$(git log -1 --format=%ct) -t face-hunger:1.0.0 .
#   docker run --rm -p 127.0.0.1:8765:8765 -v fh-data:/data -v /path/to/models:/models:ro \
#     -v /path/to/photos:/photos:ro -e LFS_APP_PASSWORD=... face-hunger:1.0.0

ARG NODE_IMAGE=node:22.12.0-bookworm-slim
ARG PYTHON_IMAGE=python:3.12.8-slim-bookworm

FROM ${NODE_IMAGE} AS frontend
WORKDIR /src/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --ignore-scripts --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM ${PYTHON_IMAGE} AS runtime
ARG SOURCE_DATE_EPOCH=0
ENV SOURCE_DATE_EPOCH=${SOURCE_DATE_EPOCH} PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    LFS_HOST=0.0.0.0 LFS_PORT=8765 LFS_DATA_DIR=/data LFS_MODEL_DIR=/models LFS_ALLOWED_ROOTS=/photos LFS_ALLOW_LAN=1
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/* /var/log/* /var/cache/*
WORKDIR /app
COPY requirements.lock pyproject.toml ./
RUN pip install --no-deps -r requirements.lock
COPY backend/ backend/
COPY scripts/ scripts/
COPY plugins/ plugins/
RUN find /app -exec touch -h -d "@${SOURCE_DATE_EPOCH}" {} +
COPY --from=frontend /src/frontend/dist frontend/dist
RUN useradd --system --uid 10001 --home-dir /data fh && mkdir -p /data /models /photos && chown fh /data
USER fh
VOLUME ["/data"]
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8765/api/health/live', timeout=4).status == 200 else 1)"]
CMD ["python", "-m", "backend"]
