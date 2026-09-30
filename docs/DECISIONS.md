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
