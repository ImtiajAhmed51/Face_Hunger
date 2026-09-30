# Engineering decisions

Short log of choices and dependencies. One entry per decision, newest last.

## Licensing
- **buffalo_l (InsightFace) weights are non-commercial only.** The code is MIT, but the
  pretrained face models may not be used commercially. New models added by this project are
  Apache-2.0 (SigLIP 2, DINOv2) only.

## Backend structure
- **App factory + routers (`backend/app.py`, `backend/routers/*`).** `backend/__main__.py` only
  builds the default app. Services live in one `Services` container; routers reach them via
  late-bound proxies in `backend/deps.py`, so maintenance actions that reopen the face store are
  visible everywhere without module globals.
- **API contract snapshot.** `tests/fixtures/api_baseline.json` was captured from the monolithic
  `__main__.py` before the split; `tests/test_api_contract.py` fails on any removed or changed
  operation. New endpoints are allowed; changed ones are not.

## Tooling
- **ruff** (dev only): Python lint (`F`, `E9`, import order). Fast, single binary, no runtime cost.
- Frontend "lint" is `tsc -b` in strict mode with `noUnusedLocals/Parameters`; no ESLint, to keep
  the dev toolchain small.

## Embeddings and models
- **usearch** (Apache-2.0): HNSW vector index with incremental add/remove, f16/i8 storage and
  sub-10 ms top-1000 search at 100k x 768. FAISS stays an optional extra, used only by the
  existing duplicate grouping when installed.
- **tokenizers** (Apache-2.0): loads SigLIP 2's `tokenizer.json` (Gemma SentencePiece) without
  pulling in `transformers`/torch.
- **Stores are keyed by `model_id@version:dim`** (`backend/vectors/specs.py`). A new model or
  version gets a new file (`data/vectors/<key>.f32`), rows in `media_vectors` and its own index;
  nothing existing is rewritten. Search uses the newest space for a role once it covers >=95% of
  the library, otherwise the best-covered one.
- **The DB row is the commit point.** Vectors are fsynced before their row is inserted and
  `UNIQUE(model_key, media_id)` makes retries idempotent, so kill -9 leaves only unreferenced
  bytes (partial tails are truncated on open). ANN indexes are caches with a row-id watermark:
  on load they catch up on new rows, drop deleted keys, and rebuild only when they cannot.
- **Legacy stores adopted in place**: the torch-hub DINOv2 vectors (`media_embeddings.bin`,
  768-D) and ArcFace face vectors (`embeddings.bin`) are registered read-only; migration 6 copies
  only offsets. The old torch.hub DINOv2 loader (which could download weights) is gone.
- **DINOv2-small by default** (384-D, ~88 MB fp32): fast enough to backfill 500k items on CPU and
  plenty for near-duplicates. `LFS_DINO_VARIANT=base` switches to the 768-D model.
- **Visual similarity by face id uses ArcFace**, not DINOv2: a DINOv2 vector of a face crop
  compared with whole-image vectors is meaningless, so "find similar" for a face means "same
  identity" via the face space's own index.
- **int8 weights run on CPU.** CoreML accepts the quantized SigLIP 2 graph but fails at run time;
  int8 models therefore skip accelerators, and any model that fails at run time on an accelerator
  is transparently re-created on the next provider (CUDA -> CoreML -> DirectML -> CPU).
- **RAM**: SigLIP 2 vision+text int8 plus DINOv2-small peaked at ~700 MB RSS for the whole
  process on an M-series Mac. Each ONNX graph unloads after `LFS_MODEL_IDLE_SECONDS` (300).
- **Migrations**: versioned list in `backend/migrations.py`; a consistent SQLite backup is written
  to `data/backups/` before the first pending migration runs on an existing database.

## Hybrid search
- **Weighted RRF (k=60)** fuses ranked lists from text (SigLIP 2), similar-media (visual space),
  similar-face (ArcFace space) and optional recency. Filters are SQL and restrict every signal.
  Rank fusion avoids calibrating SigLIP, DINOv2 and ArcFace similarities against each other.
- **"Quality threshold" = best face quality in the media item** (`faces.quality`), because there
  are no per-media quality scores yet. Media with no faces fail a quality filter.
- **Filter sets of 5,000 ids or fewer are scored exactly**; larger ones are applied after the
  ANN search with widening k. This keeps selective queries exact and broad ones fast.
- **Face similarity falls back to a memory-mapped scan** of `embeddings.bin` while the face HNSW
  index builds in the background (~141k faces in the reference library).
- **API compatibility check relaxed from "identical" to "additive"**: existing operations,
  parameters and fields must remain unchanged, and new ones must be optional. Item 1 was
  verified against the identical form before the relaxation.
- `/api/search/parse` keeps its keys and adds `text`, `filters` and `embedding_query`. It also
  parses dates ("June 2021", "since 2018", "last year") and multi-word names.

## Job system
- **One queue, one runner.** New job kinds (`ingest`, `embed_backfill`, `rebuild_index`) run one
  at a time from the existing `jobs` table, lowest `priority` first (10 urgent, 50 normal,
  80 background). A single runner keeps model RAM and SQLite write contention predictable on
  a 16 GB laptop. Legacy full scans (`kind='index'`) stay on the existing Worker but share the
  table, the SSE stream and a lock with `ingest`, so the two never interleave.
- **Cooperative preemption.** Background jobs check `should_yield()` between batches and go
  back to the queue with their progress when urgent work arrives. The watcher's ingest therefore
  never waits for a long backfill.
- **Crash recovery**: on start, `running` jobs are re-queued (handlers are idempotent). After 3
  interruptions a job is marked failed. `paused` and `queued` jobs are left as they are.
- **SSE instead of WebSockets**: `GET /api/jobs/events` works through the Vite proxy and needs no
  extra dependency. It polls the jobs table every 250 ms, which covers the legacy Worker too.
- **watchdog** (Apache-2.0, already installed) for file events (FSEvents on macOS, inotify on
  Linux, ReadDirectoryChangesW on Windows). Paths are debounced for 0.75 s with a stable size, so
  files still being copied are not indexed. A deleted file only marks its media row `missing`.
  Disable with `LFS_WATCH=false`.

## Frontend
- **@tanstack/react-query** (MIT) underlies `useResource`. The hook keeps its old return shape,
  so every page moved onto the query cache at once without call-site changes; `refreshData()`
  invalidates queries. **@tanstack/react-virtual** (MIT) virtualizes grids with window
  scrolling.
- **Fixed-height rows.** Virtual grids use uniform rows (square tile + fixed caption), so pages
  streaming in never shift layout. The justified "Mosaic" layout needs every item's aspect ratio
  up front, so it stays a paged view. "Grid" (virtualized, infinite) is now the default.
- **Sparse paging.** Only pages overlapping the viewport (+1 ahead) are fetched, 120 items each
  for media and 96 for people. The remembered total keeps the scrollbar stable.
  Limit: at 4 columns, ~400k items reaches Chromium's ~33.5M px element height, so libraries
  over ~400k items need more columns or date sections (timeline, Phase 2).
- **Blur-up** uses a separate `GET /api/media/lqip?ids=` (<= 240 ids, ~400-byte 16 px JPEG data
  URLs built from cached thumbnails with DCT-scaled decode, LRU-cached in memory) rather than
  enlarging every `/api/media` response.
- **Undo** is a bounded client-side stack. Each entry holds the inverse API calls (restore after
  soft delete, re-delete after restore, `DELETE /exclusions` after excluding, review
  `decision: "reset"` after Yes/No, face restore after face delete). Ctrl/Cmd+Z runs the latest,
  and `U` works in Review. Review decisions are pushed silently so rapid reviewing stays quiet.
- **Command palette** (Ctrl/Cmd+K) is a combobox/listbox in the existing modal `<dialog>`. The
  brief referred to an existing palette, but none existed in the repo, so it is new.
- **Styles** split along the existing `@layer` blocks into `styles/tokens.css` (design tokens),
  `base.css`, `components/*.css` and breakpoint/a11y layers. The cascade is unchanged; built CSS
  was byte-for-byte equivalent in size before the new components were added.
