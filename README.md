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
│   ├── __main__.py      # FastAPI app & API routes
│   ├── db.py            # SQLite schema & migrations
│   ├── clustering.py    # Centroids / matching
│   ├── engine.py        # InsightFace load
│   ├── scanner.py       # Library scan pipeline
│   ├── embeddings.py    # Embedding store
│   ├── worker.py        # Background jobs
│   ├── threshold_tuning.py
│   └── ...
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
