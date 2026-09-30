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
