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
| **Photos** | Chronological, virtualized photo/video timeline with date scrubber |
| **Media browser** | Filter, sort, select and organize media in mosaic or grid |
| **Albums / Favorites** | Manual albums, saved search collections, favorite media |
| **Recently added** | Media ordered by its indexing timestamp |
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

**Gallery (Media browser / Videos / No faces)**
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
LFS_HOST=127.0.0.1 python -m backend
# or, with the venv active: face-hunger
```

Open the URL printed in the terminal (typically `http://127.0.0.1:8765`).

With this project's existing virtual environment, run `LFS_HOST=127.0.0.1 .venv/bin/python -m backend` from the project root.
See [the redesign audit](docs/REDESIGN_AUDIT.md) for verified workflows, performance measurements and remaining coverage limits.
The shared UI now uses an iOS-inspired Liquid Glass material in light and dark themes; see [the glass presentation audit](docs/LIQUID_GLASS_AUDIT.md) for responsive coverage and browser limitations.

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
cd frontend && npx playwright install chromium --only-shell && npm run e2e  # browser tests (once: install)
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

## Adding photos from a Windows PC or NAS

Face Hunger can index a folder that another computer shares on your network. It only ever
**reads** from the share: thumbnails, faces, search data and edits are stored on the computer
running Face Hunger. If the other computer is switched off, everything already indexed stays
searchable and the library shows as *Offline* until it is back.

### On the Windows PC (the computer that has the photos)

1. Make an account for sharing (recommended): Settings > Accounts > Other users > Add account >
   "I don't have this person's sign-in information" > "Add a user without a Microsoft account".
   Give it a name (for example `photos`) and a password.
2. Right-click the photo folder > Properties > Sharing > **Advanced Sharing** > tick
   "Share this folder". Note the **Share name**.
3. Click **Permissions**, remove "Everyone", add the `photos` account and tick only **Read**.
4. On the **Security** tab of the same Properties window, make sure that account has "Read & execute".
5. Settings > Network & Internet > Advanced network settings > Advanced sharing settings: turn on
   **File and printer sharing** for Private networks. The network must be set to *Private*.
6. Find the PC's name (Settings > System > About > Device name) or IP address (`ipconfig`).

For a NAS, create a read-only user for the photo share in its admin page and enable SMB.

### On the Mac running Face Hunger

1. Open **Libraries** > **Add library**. A folder chooser opens; you never type a path.
2. Under *Network shares* click **Connect to network share**. Enter the PC name or IP address, the
   share name, the user name and password. Tick "Remember in Keychain" if you want reconnecting
   to work without asking again. (**Discover on network** lists computers that announce
   themselves; Windows PCs often do not, so typing the name is normal.)
3. The chooser opens the share. Pick the folder (or tick several), check the summary, and press
   **Select this folder**.
4. Press **Scan now** on the new library. The first scan reads every photo and video; later scans
   only read new and changed files. Network libraries are checked every 15 minutes (change this
   under the library's Settings), because file-change notifications do not work over SMB.

A share you already connected in Finder (Go > Connect to Server, `smb://PC-NAME/share`) appears
under *Network shares* automatically.

### If something goes wrong

| What you see | What to check |
| --- | --- |
| "The user name or password was not accepted" | The account exists on the PC, has a password, and is in the share's Permissions. Try `PC-NAME\\photos` as the user name. |
| "That computer could not be reached" | The PC is on and awake, both are on the same network, the name is right (try the IP address). |
| "The computer answered but refused the file-sharing connection" | File and printer sharing is off, or the Windows firewall blocks it (allow "File and Printer Sharing"). |
| "That computer has no shared folder with this name" | Use the exact Share name from the Sharing tab, not the folder's path. |
| "This account is not allowed to open that shared folder" | Add Read permission on both the Sharing and Security tabs. |
| "The two computers could not agree on a file-sharing version" | The device only offers SMB1. Enable SMB2 or newer on it. |
| Library shows *Offline* | The PC is asleep or off, or the Mac left the network. Nothing is deleted. Press **Reconnect**; it also recovers by itself. |
| The share came back under another name (`/Volumes/Photos-1`) | Face Hunger follows it automatically when it knows the share. Otherwise use **Relink** and pick the folder: a sample of files is compared first, and nothing is re-indexed. |
| Folders cannot be listed on the Mac | System Settings > Privacy & Security > Files and Folders (or Network Volumes): allow the app that runs Face Hunger (for example Terminal). |
| Scans are slow | Wired network beats Wi-Fi. Lower or raise "Files read at the same time" in the library's Settings; turn off "Scan videos" if you only want photos. |

Limits: connecting from inside the app is implemented for macOS (and through `gio` on Linux,
`net use` on Windows, both untested here). Shares are mounted read-write by macOS when you give a
password; Face Hunger still refuses to write to them, with one exception: **Permanent delete** (Deleted tab, after typing DELETE) removes the original on the share. Give the share account read-only access if you never want that to be possible.

---

## License

Personal / private use. Add an explicit license file if you publish the project publicly.
