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
