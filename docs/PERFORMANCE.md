# Performance

Measured numbers only. Machine: Apple Silicon (M-series) MacBook, 16 GB RAM, macOS, Python 3.13,
CPU execution unless noted. Re-run the commands to update.

## Hybrid search (`POST /api/search/hybrid`)

`python scripts/bench_search.py --items 100000 --rounds 10` (also `pytest -m perf`)

Synthetic library: 100k media (10% video), 60k faces over 500 people, 768-D text space and
384-D visual space in usearch (f16). 90 timed queries after warm-up, full request path
excluding HTTP (SQL filters, ANN/exact scoring, RRF, batched serialisation of 60 items).

| Query mix | p50 | p95 |
|---|---|---|
| All 9 query shapes | **23 ms** | **87 ms** (budget 300 ms) |
| text only | 10 ms | 13 ms |
| text + kind | 33 ms | 38 ms |
| text + person | 21 ms | 29 ms |
| text + 2-year date range | 87 ms | 95 ms |
| text + 2 people + face quality | 41 ms | 50 ms |
| filters only: person + kind | 46 ms | 49 ms |
| filters only: date range | 23 ms | 24 ms |
| similar media (DINOv2 space) | 10 ms | 11 ms |
| text + similar + recency | 18 ms | 22 ms |

Index build (one-off, incremental afterwards): 100k x 768 in 39 s, 100k x 384 in 19 s.

History: the first version had p95 327 ms. It re-counted library coverage on every query and
materialised broad filter sets (90k ids). Coverage is now cached for 5 s and invalidated on
writes, and filter sets are only materialised when they have 5,000 ids or fewer. Broader
filters are applied to widening ANN candidate pages instead.

## Models (CPU, int8 SigLIP 2 / fp32 DINOv2-small)

| Step | Cold (load + run) | Warm |
|---|---|---|
| SigLIP 2 vision, batch of 3 images | 1.05 s | 157 ms |
| SigLIP 2 text, 3 queries | 2.19 s | 54 ms |
| DINOv2-small, batch of 4 images | 0.93 s | 203 ms |

Peak RSS with all three sessions loaded: ~700 MB (whole process).

## usearch primitives (100k x 768, f16)

top-1000 search 8 ms; fetching 20k vectors 33 ms; save 1.7 s; load 0.12 s.

## Jobs and watcher (`pytest tests/test_jobs.py -s`)

- Copied-in photo -> indexed (thumbnail, hashes, faces with the stub engine): **0.97 s** in 3/3
  runs (budget 5 s). With the real buffalo_l, the first file after startup also pays the lazy
  model load (a few seconds on CPU); later files do not.
- Cancel of a running backfill whose batches take 300 ms: asserted **< 2 s** (every batch
  boundary is a checkpoint).

## Frontend

Real library (15,119 photos) in the in-app Chromium, 1280x860, production build:

- Virtualized Photos grid, programmatic scroll of 400 px per frame for 600 frames
  (24,000 px/s): **median frame 16.7 ms, p95 17.6 ms**, 3 of 600 frames over 33 ms. 40 grid
  cells and ~260 elements in the DOM regardless of library size.
- `photos 2023` from the command palette: parsed, filtered and returned 1,638 results in
  **8 ms** server time. "Find similar" on the adopted 16k DINOv2 vectors: 875 ms on first use
  (builds the HNSW index), then **138 ms**.
- Bundle: initial JS **141 KB gzip** (budget 250 KB), CSS 22.5 KB gzip.

## Health (real library: 16,076 media, 141,432 faces, 16,075 legacy DINOv2 vectors)

- Full integrity check: **3.1 s** (stat of every original, 2 x 5,000 checksum samples across
  365 MB of embeddings, SQLite `quick_check` on a 113 MB database, index load). Result: no errors;
  one original missing from disk that was not yet flagged.
- First `GET /api/health/library`: 4.5 s, walking ~2 GB / ~140k thumbnail files. Cached for
  5 minutes afterwards (67 ms).

## Formats (CPU, best of 3)

| Sample | Index decode (<= 4096 px) | Viewer preview, uncached (<= 2560 px) |
|---|---|---|
| Nikon Z 7 NEF, 45.7 MP | 157 ms | 219 ms |
| Canon EOS R5 CR3, 45 MP | 149 ms | 215 ms |
| Canon 5D III CR2, 22 MP | 408 ms | 120 ms |
| Sony A7 III ARW (1616 px embedded preview) | 27 ms | 20 ms |
| Pixel 3a DNG (small preview, half-size demosaic) | 212 ms | 198 ms |

- 45.7 MP NEF after indexing: `GET /api/media/{id}/preview` **max 3.5 ms** over 5 requests (cached at
  index time; budget 300 ms).
- The first version decoded 45 MP previews in full (430-900 ms). Pillow's `draft()` needs both
  dimensions at least the requested size, so a square box disabled DCT scaling for 3:2 images.
  Aspect-correct requests fixed it.
- The fixture library with real buffalo_l (6 formats of a 6-person group photo plus 7 RAW files)
  indexes end to end in about 10 s.

## Timeline and map (synthetic 100k library: `scripts/make_synthetic_library.py`)

- **Timeline, 100,000 items** (3.86M px tall, sections from 1,6xx days): 600 frames of
  programmatic scrolling at 24,000 px/s, **median 16.7 ms, p95 17.5 ms, max 17.7 ms, 0 frames
  over 33 ms**, ~420 DOM elements. Jumping to 2016 with the scrubber (End) and scrolling back:
  median 16.7 ms, p95 17.6 ms, no skeletons left after settling.
- **Map, 50,000 geotagged points**: MapLibre constructed -> clustered and idle in **446-460 ms**
  (budget 1 s). Cold page load -> idle map, including the 1.28 MB points download (213 ms),
  in 1.07 s. All requests went to the app's own origin (plus a blob: worker); zero external
  requests with tiles off.
- **Metadata backfill on the real library**: 16,076 items in 82 s (~196/s) on CPU; 2,425
  geotagged; 7,323 dated from file name or mtime.

## Quality scoring

- Burst fixture (50 groups x 5 frames): **94% top-1** (seed 11); mean 93.5% over seeds 0-7, worst
  88%. Mild degradations: 64% (seed 12 fixture), 69% mean.
- Real library, CPU, face landmarks re-detected on the fly for pre-existing faces: **14.8 media/s**
  steady state (16,076 items in about 18 min in the background, lowest priority).

## Events (real library: 10,684 datable items of 16,076)

- Full detection from scratch: **0.46 s** -> 1,950 events; unchanged re-run 0.19 s; switching
  rule versions (removing the giant bulk-copy events) 0.19 s. In the live app, the job completes
  in ~1 s.
- Cover refresh was a correlated subquery per event (5.1 s for 2k events, and it competed with the
  quality-scoring job's writes). It is now one window-function query.

## Video (CPU, buffalo_l at 640 px, `video_interval` 3 s)

- **10-minute 1080p H.264** (synthetic, 2 faces, `pytest -m perf tests/test_video.py`): faces +
  tracks + edge refinement **49.9 s**, keyframes **5.9 s**, **~56 s total** (budget 300 s).
  Progress streams over SSE as `video_seconds / video_duration`.
- Cancel mid-video: the job stops within 2.5 s (asserted) and leaves nothing half-committed.
- Fixture with 2 known people (60 s, 3 + 1 appearances): 2 people from >= 3 tracklets, every moment
  within +-2 s of the ground truth.
- Real library keyframes (mostly long 1080p/4K files): 9 videos (largest 44 min 1080p) in ~10 min
  in the background. The full 957-video library takes several hours at the lowest priority.
- Scene detection on the cut test clip finds 0 / 10 / 25 / 40 s exactly.

## Duplicate resolution (UI fixture: `scripts/make_ui_fixture.py`)

- 20 exact-duplicate groups (27 copies): predicted savings 2,973,000 bytes; after a keyboard-only
  resolve with "free space", the library folders shrank by **exactly 2,973,000 bytes**. Cmd+Z
  restored all 50 files **byte-identical** (SHA-256 of every file compared) and emptied the bin.

## Frontend bundle after Phase 2

- Initial JS **153.4 KB gzip** (budget 250 KB). Lazy chunks: Map 294.5 KB (MapLibre + pmtiles),
  Timeline 3.1 KB, Health 2.7 KB, Events 2.2 KB, Albums 2.1 KB, duplicates resolver 2.9 KB.
- Playwright suite (9 tests, fixture build + server start included): ~24 s, 3/3 runs green.

# Phase 3

## Edit tools
- External sidecar change (another app rewrites the `.xmp`): imported **0.99 s** after the write
  (budget 5 s), including the watcher's 0.75 s debounce.
- XMP written by the app: `exiftool 13.55` reads Rating / Label / Orientation / HasCrop /
  Crop* exactly and `exiftool -validate` reports no warnings.

## Safe sharing (real buffalo_l, CPU)
- Fixture of 3 photos with 14 faces: **14/14 targeted faces below the 0.48 match threshold** in
  the outputs for blur, pixelate and mask at the weakest slider position (detection re-run on
  every output). A kept person still matches. Outputs contain no EXIF/GPS/XMP (exiftool).
- Export of 2 photos (8 faces, with fresh detection and verification): 0.3 s once the engine is loaded.

## Encrypted packages
- Export peak memory above baseline (subprocess, `ru_maxrss`): **49 MB for a 67 MB package,
  38 MB for a 671 MB package**: 10x the data, no growth. The cost is scrypt (32 MB), one 1 MiB
  chunk and I/O buffers, so a 50 GB export needs the same memory.
- 671 MB export with originals: ~2 s on this machine (AES-GCM is hardware accelerated).
- Round trip onto a clean data dir (12 photos, 8 faces, 2 people, album, edits, text vectors):
  people, albums, favourites, edits and 4 search queries identical.

## Local VLM (SmolVLM2-500M, int8/q4, CPU)
- Load: **0.7-0.8 s**; memory when loaded: **406-462 MB** (cap 4 GB); disk 379 MB.
- One-line caption of a photo: ~1.9 s; album title: ~0.6 s; 3 search expansions: ~1.5 s.
- **Album from a prompt on a 10,000-item library** (retrieval, diversity pick of 30, title,
  8 captions): **19.3 s** (budget 120 s). Unloads after the idle period (asserted with 5 s).
- With the VLM disabled, the same album flow (rules only, no captions) completes in well under 1 s.
