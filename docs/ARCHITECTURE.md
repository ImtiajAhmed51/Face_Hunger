# Architecture

One process, one data folder, no network. A FastAPI server (`backend/`) serves a JSON API and the
built React app (`frontend/dist`); everything else is files under `LFS_DATA_DIR`.

```
browser (React, TanStack Query, virtualised grids)
   │  /api/*  JSON, SSE for job progress, media bytes
   ▼
RequestId → Security (host, origin, CSRF, app lock, headers) → routers/*      backend/app.py
   ▼
services/container.py  Services: one object graph per process
   ├─ db.py + migrations.py        SQLite (WAL), versioned idempotent migrations, backup before each
   ├─ embeddings.py                append-only face vectors (embeddings.bin)
   ├─ vectors/                     one store + usearch ANN index per model (ModelSpec key)
   ├─ jobs/                        priority queue in SQLite, resumable handlers, file watcher
   ├─ ml/                          lazy ONNX sessions (SigLIP 2, DINOv2, optional SmolVLM2), idle unload
   ├─ engine.py                    face detection/recognition (buffalo_l via onnxruntime)
   ├─ services/*                   search, library (albums, audit, undo), dedupe, edits, sharing,
   │                               packages, assistant, storage, plugins
   ├─ plugins/                     manifest, subprocess host, sandboxed runner
   └─ ops/                         logging, health, backup/restore, fhpack, diagnostics
```

## Data folder

| Path | Contents |
| --- | --- |
| `index.sqlite` | everything relational: media, faces, people, albums, events, edits, jobs, audit log, settings |
| `embeddings.bin` | face embeddings, append-only, checksummed per row |
| `vectors/<model>.f32`, `.usearch` | per-model media vectors and their ANN index |
| `thumbnails/`, `previews/`, `video_cache/` | derived images; safe to delete, rebuilt on demand |
| `sidecars/<id>.json` | edit sidecars (an `.xmp` is also written next to the original) |
| `duplicate-bin/<audit id>/` | originals moved out by cleanup, restorable by undo |
| `exports/` | backups (`.zip`) and encrypted packages (`.fhpack`) |
| `backups/` | automatic pre-migration database copies and pre-restore data |
| `plugins/`, `plugin-data/` | installed plugins and their private storage |
| `diagnostics.sqlite` | only if diagnostics were turned on |

## Principles

- **Originals are read-only.** Edits are data; exports are new files.
- **Every model has its own store.** A new model or version gets a new key and is built beside the
  old one; switching is a setting, rolling back is clearing it.
- **Schema changes are migrations** (`backend/migrations.py`, currently 6-16): idempotent, ordered,
  with an automatic database backup first. `tests/test_upgrade.py` upgrades real Phase 1 and
  Phase 2 data folders.
- **Long work is a job**: persisted, prioritised, resumable after a crash, cancellable, reported
  over SSE. Handlers checkpoint and yield to urgent work.
- **Every destructive action is logged with its undo** (`services/library.py`).
- **Optional things are lazy**: models load on first use and unload when idle; the VLM, plugins and
  diagnostics do nothing until the user turns them on.

## Request path and security

`backend/security.py` runs before routing: Host allow-list (DNS rebinding), Origin check and the
`X-LFS-Request` header on every state-changing call (CSRF), the optional app lock (session cookie),
then security headers and a CSP on HTML. Handlers validate paths against the allowed roots
(`LFS_ALLOWED_ROOTS`) and never build file paths from request input without a containment check.
See docs/SECURITY.md.

## Search

`services/search.py`: SQL filters, then vector signals (text, expansions, similar image, similar
face), caption and plugin-label matches and plugin signals, fused with weighted reciprocal rank
fusion. Small filtered sets are scored exactly; large ones use the ANN index.

## Frontend

React 19 + Vite, hash routing, code-split screens (`src/pages`), TanStack Query for data, a
virtualised grid for large lists, an undo stack bound to the server's audit log, and an i18n table
(English, Bangla) enforced by a test that rejects hard-coded strings in screens.
