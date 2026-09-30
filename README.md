# Face Hunger (local-face-search)

**Private, on-device face search and clustering** for your photo and video libraries.  
Your original files never leave the machine. The app only builds a local index (faces, embeddings, people).

---

## What is in this project

| Area | Description |
|------|-------------|
| **Backend** | FastAPI + SQLite + InsightFace (buffalo_l) + OpenCV |
| **Frontend** | React + TypeScript + Vite |
| **Privacy** | Local-only; no cloud upload of your media |

### App tabs / pages

| Tab | What it does |
|-----|----------------|
| **Overview** | Dashboard: counts, recent people/media, scan progress |
| **People** | All person clusters — sort by faces/photos/videos/name |
| **Clusters** | Visual mosaic of each person with sample face tiles + zoom |
| **Photos** | Photo gallery (mosaic or grid) |
| **Videos** | Video gallery with hover preview |
| **No faces** | Media where no face was detected |
| **Search** | Search by people / text filters |
| **Review** | Confirm or reject uncertain matches (keys: `Y` / `N` / `D` / `V`) |
| **Cleanup** | Duplicate suggestions, deleted faces, maintenance |
| **Settings** | Libraries, matching/review thresholds, detection size, theme |

### Features

**Library & indexing**
- Add local folders as libraries and scan photos/videos
- Face detection, embeddings, incremental clustering
- Soft-delete and permanent purge of originals (with confirmation)
- Move a person's media to another folder and remove them from the index

**People**
- Sort: most faces / photos / videos / name
- Rename, merge, export
- **Skip identification** — soft-remove that person's faces from the active index (files stay on disk); restore later
- Filter list: *To identify* / *Skipped only* / *Everyone*
- Bulk select + skip / resume

**Gallery (Photos / Videos / No faces)**
- **Mosaic** collage layout (mixed tile sizes) or classic **Grid**
- Sort: newest, size largest/smallest, name
- File size badge + resolution on cards
- **Hide people** — exclude media that contains selected persons
- Multi-select: Shift+click range, paint-drag on checkboxes
- Selection persists across pages
- Permanent delete of originals (type `DELETE` to confirm)
- Video: hover for muted in-card preview

**Review**
- Fast Yes/No (optimistic UI)
- Assign to someone else, soft-delete face

**Settings**
- Matching threshold, review threshold, auto-tune from reviews
- Detection size, video sampling interval
- Theme (system / light / dark)

---

## Project layout

```
local-face-search/
├── backend/
│   ├── __main__.py      # Entry point (python -m backend)
│   ├── app.py           # FastAPI app factory
│   ├── routers/         # One APIRouter per area (media, people, search, jobs, health, ...)
│   ├── services/        # Service container, presenters, hybrid search
│   ├── schemas.py       # Pydantic request bodies
│   ├── migrations.py    # Versioned migrations (automatic backup first)
│   ├── vectors/         # Multi-model embedding stores + usearch indexes
│   ├── ml/              # ONNX runtime: SigLIP 2, DINOv2, provider selection
│   ├── jobs/            # Priority job queue, handlers, file watcher
│   ├── ops/             # Structured logging, health checks, backup/restore
│   ├── worker.py        # Full-library scans
│   └── ...
├── scripts/fetch_models.py   # One-time manual model download
├── scripts/bench_search.py   # Hybrid-search latency benchmark
├── docs/DECISIONS.md, docs/PERFORMANCE.md
├── tests/               # pytest (pytest -m perf for benchmarks)
├── frontend/
│   ├── src/
│   │   ├── App.tsx
│   │   ├── pages/       # Home, People, Clusters, Review, ...
│   │   ├── components/  # MediaGrid, MediaViewer, ...
│   │   └── ...
│   └── package.json
├── pyproject.toml
├── .gitignore           # Excludes data, DB, node_modules, media
└── README.md
```

**Not in the repo (keep local only):**
- `data/`, `*.db`, embeddings, thumbnails
- `node_modules/`, `frontend/dist/`
- Your real photo/video folders

---

## Requirements

- **Python** 3.11 – 3.13
- **Node.js** 18+
- macOS or Linux

---

## Setup

### Backend

```bash
cd local-face-search
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e .
```

Optional faster search:

```bash
pip install -e ".[faiss]"
```

### Frontend

```bash
cd frontend
npm install
npm run build
```

### Optional models (text search, visual similarity)

```bash
python scripts/fetch_models.py   # SigLIP 2 + DINOv2-small into ./models (Apache-2.0)
```

Nothing is ever downloaded at runtime. Without these models, face search, filters and
duplicates by hash all keep working.

### Run

```bash
# from project root, with venv active
local-face-search
# or: python -m backend
```

Open the URL printed in the terminal (typically `http://127.0.0.1:8765`).

After UI code changes:

```bash
cd frontend && npm run build
```

---

## Checks

```bash
ruff check backend tests scripts && python -m pytest -q        # backend
python -m pytest -m perf                                       # 100k search benchmark
cd frontend && npx tsc -b && npx vitest run && npx vite build  # frontend
```

## Operations

- `GET /api/health` (liveness), `GET /api/health/library` (storage, integrity), Health page in the app.
- Backups: Health page, `POST /api/backup/export`, or `python -m backend.ops.backup export`.
  Restore is verified, staged, and applied on the next start; your current data is moved to
  `data/backups/pre-restore-*`, never deleted.
- Logs are JSON lines with a `request_id` (set `LFS_LOG_FORMAT=text` for plain text).
- `LFS_HOST=127.0.0.1` keeps the server off your local network.

## Recommended settings (starting point)

| Setting | Suggested | Notes |
|---------|-----------|--------|
| Matching threshold | **0.48 – 0.52** | Lower merges more (more errors); higher is stricter |
| Review threshold | **~0.62** | Below this → Review queue |
| Avoid | **0.30** matching | Too aggressive; floods Review with bad matches |

Use **Auto-tune from reviews** after many Yes/No decisions.

---

## Privacy

- Index and embeddings live only under your local data directory.
- Originals are only read (or deleted/moved when *you* confirm).
- Nothing is designed to upload your library to a remote server.

---

## License

Personal / private use. Add an explicit license file if you publish the project publicly.
