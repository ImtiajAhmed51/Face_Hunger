# Face recognition pipeline audit — October 11, 2026

Implementation remains within the existing Python/SQLite/InsightFace architecture. Original media were only read. Tests and experiments used disposable databases, except explicitly read-only measurements of existing photos and embeddings. Existing unrelated working-tree changes were retained.

## Baseline and retained models

- InsightFace 0.7.3, buffalo_l `det_10g.onnx`, loaded through the installed `RetinaFace` implementation. Multiple detections retain confidence, original oriented-image boxes, and five landmarks. Default input resolution remains 640; Settings supports 320/640/960 and optional multiscale detection.
- ArcFace `w600k_r50.onnx`, installed `ArcFaceONNX` implementation. Actual ONNX output and inference were verified as **512 dimensions**, with a 112×112 input. InsightFace `norm_crop` aligns five landmarks; preprocessing uses RGB, mean/std 127.5; stored vectors are L2-normalized float32. Matching uses cosine similarity / inner product, not cosine distance.
- No models or dependencies were downloaded or replaced. Existing embeddings were retained.
- Installed environment: Python 3.13, NumPy 2.5.3, ONNX Runtime 1.30.0, OpenCV headless 4.14.0.94, usearch 2.26.4, FAISS CPU 1.15.1, scikit-learn 1.9.1.
- Available ONNX providers: CoreML, Azure, CPU. CPU and CoreML inference were exercised. CoreML session selection allows CPU fallback; it does not prove that every operation executes on GPU/ANE. The misleading GPU/ANE label was removed.

Recognition SHA-256: `4c06341c33c2ca1f86781dab0e829f88ad5b64be9fba56e56bc9ebdefc619e43`.
Detector SHA-256: `5838f7fe053675b1c7a08b633df49e7af5495cee0493c7dcf6697200b85b5b91`.
These fingerprint the installed files; they are not evidence of historical download provenance.

## Implemented fixes

1. **Incremental index consistency.** A transactional SQLite change journal tracks face/media-vector insertion, embedding replacement under an existing ID, and hard deletion. Search synchronizes pending changes. Owned writes consume their journal cursor immediately; cached indexes fold new faces once rather than through both the journal and row watermark. Startup also reconciles IDs, including equal-count but different-ID caches. Replacing a face vector previously left cosine 0.0 after sync; the regression now returns its correct persistent face ID with cosine 1.0.
2. **Durable cache validation.** Persisted indexes include format version, dimension, dtype, metric, model key, sequence cursor and SHA-256. Index and metadata files are individually fsynced and atomically replaced. A crash between publication steps fails validation and rebuilds the cache from authoritative records. Embeddings are never regenerated just to repair an index.
3. **Correct fallback search.** Exact fallback ranks active database face records in bounded batches, rather than orphaned/obsolete bytes in an append-only file. Rejected, missing and excluded records are filtered. Corrupt query embeddings produce a recoverable warning. Shutdown keeps the vector descriptor open until background synchronization exits.
4. **Model contract.** `data/face-model.json` records recognition hash, actual dimensions, alignment, input configuration and normalization. Incompatible model/preprocessing replacement is refused before indexing writes new embeddings. Backups/restore preserve this sidecar. Legacy provenance is explicitly marked unverified. A different recognition model requires a separate validation store and a planned side-by-side migration; this task did not migrate models.
5. **Alignment/inference safeguards.** Invalid landmark shapes, nonfinite values, degenerate geometry, implausible crops, tiny boxes and invalid embeddings are rejected. A malformed crop does not discard other usable faces. ONNX worker threads are bounded, CPU remains available, and runtime telemetry events are disabled.
6. **Calibration integrity.** Automatic confirmations no longer count as human labels. New explicit reviews record candidate-person scores in a separate feedback table. Legacy reassignment scores are excluded. Reassignment clears obsolete similarity, and undo restores it. Calibration needs 40 scored explicit reviews, at least 10 in each class, finite scores, and a predictive threshold in the supported range. Returned metrics are marked provisional, not held-out validation. Leave-one-out evaluation now actually excludes the query face from its own centroid.
7. **Clustering and review.** Streaming sums replace retention of every face vector during centroid refresh. Explicit reconciliation keeps isolated unconfirmed singleton noise unassigned in Review, with an audit entry and undo. Named/manual identities and established multi-face identities are preserved. Duplicate-frame observations stay associated with video tracks.
8. **Video processing.** Embeddings from a frame are appended with one fsync. Consecutive frames with exactly equal decoded pixels reuse detections, embeddings and crops while retaining each timestamp and tracking observation. A one-pixel change forces fresh inference. Existing adaptive intervals, supported formats, edge refinement, cancellation and atomic per-media publishing remain.
9. **Fast job completion.** The browser now refreshes when a new job is first observed already completed. This fixed an actual backup-download browser failure.

## Search indexes and clustering parameters

Persistent face search remains **usearch HNSW, cosine distance converted to similarity, i8 vectors**, keyed by persistent SQLite face IDs; authoritative embeddings remain float32. Media spaces use f16. Connectivity 16, insertion expansion 64, search expansion 96 were retained. Updates are batched; a full cache rebuild is only needed for an incompatible/missing/corrupt cache or explicit rebuild.

FAISS previously supplied in-memory `IndexFlatIP(512)` centroid matching and `IndexHNSWFlat` reconciliation neighbors, not the persisted face search index. The installed Mac FAISS/Torch wheels bundle separate OpenMP libraries. Experiments produced a native abort and later a libomp segmentation fault. A runtime guard now uses the existing exact NumPy centroid/neighbor fallback for this conflicting installation. It never sets `KMP_DUPLICATE_LIB_OK`. FAISS remains optional on compatible environments; no server vector database was added.

Incremental quality-weighted centroid matching remains separate from explicit centroid-graph DBSCAN. DBSCAN uses precomputed cosine distance, `min_samples=2`, `eps=1 - matching_threshold + 1e-6`, bounded neighbors (64) and 20,000 eligible-centroid safety limit. Matching threshold **0.48** and its existing variance adjustment remain provisional inherited settings; this audit did not establish an optimal threshold or validate cluster purity.

## Measurements

| Measurement | Before | After |
|---|---:|---:|
| 10,000-face centroid temporary Python memory | 21.25 MiB | 0.33 MiB |
| Same centroid refresh | 1.122 s | 0.934 s (other runs 0.88–0.98 s) |
| Existing-ID replacement search cosine | 0.0, wrong result | 1.0, correct ID |
| Six-face bundled image, CoreML median of three | 91.5 ms | 81.0 ms in identical baseline rerun |

Later provider comparison measured roughly 426 ms CPU versus 80–90 ms CoreML for the six-face sample. Blank, one-face, six-face, downscaled and blurred samples were exercised; eight additional local photos returned 1–11 detections. A tightly cropped bundled Tom Hanks image returned **zero** detections both before and after; coverage is not perfect. Profile/occlusion coverage is not exhaustively labeled.

The actual i8 face-index benchmark used 10,000 synthetic normalized vectors and 50 queries: 0.136 ms/query ANN versus 0.364 ms/query exact FAISS, top-1 agreement 100%, recall@10 **71.4%**. These are synthetic retrieval measurements, not recognition accuracy or production-library recall. Earlier f16 experiments were not the production face-index configuration.

Eight manually reviewed existing photo faces matched fresh embeddings at cosine **0.987–0.999**, with box IoU 0.993–0.999. This supports current-model reuse for those samples, not proof for every historical record.

A read-only convenience sample of 11 named manual groups yielded 106 cross-media positive pairs and 55 negative pairs. Thresholds 0.40–0.68 were evaluated. At 0.48, nominal missed matches were 97.2% and false matches 0%; at 0.68 all nominal positives were missed. These groups are inconsistent as calibration ground truth. They were **not** used to change settings or assignments. Their labels need review before any accuracy claim or threshold tuning.

Reproduce safe measurements: `.venv/bin/python scripts/bench_face_pipeline.py --models --library-samples 8 --review-pairs`. Model measurements run in a separate process from the direct FAISS benchmark to isolate incompatible native runtimes. Production database and embeddings are opened read-only by these measurements.

## Data safety and remaining limits

Schema 18 adds `vector_changes`, its indexes/triggers, and `face_match_feedback`; it does not replace face/person IDs or alter embedding bytes. Existing databases receive an automatic SQLite backup before migration. All manual assignments, person names, original media and old embeddings are retained.

Video jobs resume at media boundaries. Interrupted video staging retries the current video; durable mid-video frame checkpointing is still absent. Changed recognition-model migration is safely refused but is not implemented as a new resumable migration wizard. The change journal has no automatic compaction yet. Approximate search recall, real-world lookalikes, severe profiles/occlusion, very large clustering memory and cluster purity require a curated labeled collection. Installed InsightFace/scikit-image emit an alignment deprecation warning; Vite retains its existing chunk-size warning.

An open browser progress stream is cancelled after the existing five-second graceful-shutdown deadline; Uvicorn logs a cancellation traceback during that restart. Application shutdown completes and persisted state remains intact. This shutdown logging behavior remains unresolved.

## Executed verification

- Critical pipeline/vector/search regression subset: **53 passed**.
- Complete backend regression suite: **352 passed, 2 performance tests deselected**; those two tests passed in their separate performance run.
- Both performance tests passed: hybrid search p95 below the existing 300 ms budget with 100,000 items, and ten-minute 1080p video processing below its 300 s budget. Face detection/tracking took 54.7 s and keyframe analysis 59.9 s; fixture setup and cleanup are excluded from these stage timings.
- Frontend unit tests: **76 passed**. TypeScript/Vite production build passed. Browser suite: **38 passed, 1 skipped** (opt-in Lighthouse test), including desktop/mobile accessibility, navigation, media viewer/video keyboard controls, face confirmation/rejection/reassignment/merge/split/undo, library forms, search and backup download. These workflows used disposable fixture data.
- Ruff and `git diff --check` passed. Normal documented startup returned HTTP 200 on disposable data; shutdown with an open progress stream completed in 5.22 s.
- Before live restart, the production snapshot remained exactly unchanged: 16,145 media, 165,160 faces, 3,494 people, one library, and 365,297,664 embedding bytes. Full embedding SHA-256 and hashes of all manual assignments and person names matched the baseline.
- After restarting the real application, all those counts and hashes still matched. Schema 18 and its pre-migration SQLite backup were verified. Root, Health, Media, People, Review and Models endpoints returned HTTP 200; metadata and face-similarity search returned valid persistent media IDs. A direct model load verified CoreML with CPU fallback and the persisted legacy-unverified model contract without changing embeddings. Cold face search used the safe exact fallback while the ANN cache initialized (10.8 s).
- A second real restart verified the persisted 165,160-face cache and identical integrity hashes. Face-search end-to-end latency was 16.0 s cold and 3.7 s warmed while the normal watcher-triggered indexing job ran. Those live response times include database/result assembly and are distinct from the synthetic vector-query benchmark; further live search profiling remains useful.
- One previously queued video-analysis job reported an unavailable source file on the mounted media volume. It was not deleted or used to fabricate a successful video test. Native in-app browser inspection was rejected by the browser URL policy; live verification used API checks, while interactive verification used the completed Playwright fixture suite.

Launch from the project directory:

```sh
LFS_HOST=127.0.0.1 NO_ALBUMENTATIONS_UPDATE=1 .venv/bin/python -m backend
```
