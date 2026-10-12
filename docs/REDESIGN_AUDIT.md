# Face Hunger redesign audit — 2026-10-10

## Implementation and data protection

Worked in the existing FastAPI/SQLite/React/TypeScript/Vite application. Existing ONNX face detection, embeddings, clustering, usearch/FAISS indexes, incremental scans, watcher, queued jobs and server-sent progress remain in place. No runtime dependencies or architecture were replaced. Optional RAW and declared test/dev packages were installed in the existing virtual environment to run the available checks.

The checkout already contained extensive local changes. Those were inspected and preserved; a pre-change binary diff was saved in `/tmp/face-hunger-before-redesign.patch`. No original media were moved, renamed, converted, compressed or deleted during this work. No embeddings, databases or indexes were discarded or rebuilt. Mutating browser tests use a synthetic library under the system temporary directory, with inference disabled and valid synthetic vectors for corrections. The fixture generator now refuses destinations outside a dedicated temporary subdirectory.

The real database passed SQLite `quick_check`. Its counts were unchanged across the application restart: 16,145 media records, 165,160 faces, 3,494 people and one library. These are record counts, including deleted/missing records; the active timeline reported 16,142 items. Count preservation is not a byte-for-byte audit of every original file. The existing edit browser test separately verifies original bytes stay unchanged.

## UI and preserved features

Photos is now the combined chronological photo/video timeline at `/` and `/photos`. It uses date sections, a date scrubber, virtualized rows, bounded thumbnail pages, explicit pending/failed/missing states, and an undated section. Its viewer supports previous/next through uncached pages, photo zoom/fit, fullscreen, keyboard navigation, native video playback, face overlays and metadata. No full-resolution images are used as timeline thumbnails. First-row thumbnails load eagerly; placeholder requests now cover mounted virtual rows instead of every prefetched media page. Timeline and media grids request 384-pixel WebP previews generated from the existing cached thumbnails. A 64-entry revision-aware cache bounds memory, geometric edits remain applied, and original files and existing JPEG thumbnails remain unchanged. The original thumbnail API response remains compatible when no size is requested.

A solid-surface light/dark theme replaces layered glass decoration. Shared blue selection, neutral colors, quiet borders, consistent spacing, smaller headings, search toolbar, desktop sidebar/rail and phone navigation apply across the application. Sidebar collapse persists across reload and works at 900-pixel window width as well as large desktop widths. The date rail starts below the timeline header and stays above phone navigation, avoiding overlapping controls. Focus rings and dialog focus recovery remain accessible. Existing Bangla support is retained; new timeline status strings have translations.

Photos, Search, People, Albums, Favorites, Recently added, Videos, Events, Places, Face review, Face clusters, Libraries and Settings are directly available. Overview, Media browser, No faces, Deleted, Duplicates, Cleanup, Storage, Health, Plugins and Diagnostics remain under More tools. The previous timeline URL remains valid. Albums, saved search collections, ratings, edits, anonymized sharing, encrypted packages, local maps, event grouping, duplicate resolution, skipped faces, face exclusions, merge/split, undo, backup and diagnostic tools remain available.

## Functional fixes and cleanup

- Person faces previously fetched every source media detail and could repeat work for a single video. The new paginated `/api/people/{id}/faces` response joins names and exclusions in SQL and includes all face observations, including rejected faces and individual video tracks.
- Bulk confirmation/rejection now uses a single atomic request, refreshes representatives once per affected person, and avoids recalculating embeddings. Audit-backed undo restores prior review states and rejection records. A stale undo refuses to overwrite subsequent corrections.
- Deleted face review previously filtered to uncertain matches and hid confirmed/rejected faces and missing sources. All deleted faces can now be restored.
- Sparse viewer navigation follows the full result order in timeline, media browser, hybrid results, albums, collections, events and map results.
- Text without an installed visual model uses actual filename/folder filtering. Both legacy and hybrid endpoints search filenames and paths. Saved searches restore name/deleted filters. Repeated deep links work after Reset search; background refresh does not overwrite unsaved filters.
- Recently added uses `indexed_at`, independently of capture date, with a stable ID tie-breaker.
- Undo remains available after reviewing the last face, including the U shortcut. Merging invalidates the removed source profile without trying to fetch it again.
- Fullscreen fallback now expands the complete viewer surface, preserving toolbar and next/previous controls; expanding only the image stage previously hid those controls.
- Search input focus is visible. Sidebar More tools can collapse even when a contained route is active.
- Conversion is explicit and now has a confirmation explaining the existing behavior: generate MP4, update indexed path, preserve original beside it as `.lfs_original`. No live conversion was performed.
- Normal `python -m backend` startup now passes the existing app instance to Uvicorn, avoiding a second service container. A five-second graceful stream-shutdown deadline prevents progress connections from holding restart open. The entry-point regression and disposable open-SSE process check cover this.
- Startup preferences use `/api/settings?storage=false`, avoiding a cold recursive thumbnail-size scan. Full Settings retains its storage usage response and existing cache.
- Removed the obsolete unmatched-search warning/state, unused People pagination setter and per-source person-face request waterfall. Consolidated loaded theme overrides and stopped importing the redundant fit stylesheet. Its pre-existing untracked file was preserved. No dependency was removed without proof of disuse; no runtime dependency was added.

## Route and interaction coverage

| Area | Executed browser coverage |
| --- | --- |
| All static routes | Light/dark WCAG axe scan, main/h1/title/lang checks; phone core screens and overflow checks |
| Photos/timeline | All/photos/videos filters, date scrubber keyboard, sidebar toggle/reload, mobile More navigation, thumbnail opening, next/previous, zoom/fit, Escape; fullscreen/overlay/metadata/original export with byte comparison |
| Media/search/favorites/deleted | Filename deep link, actual matching result, favorite and reload persistence, soft-delete/restore, selection, export, filter workflows |
| People and face review | Individual-face selection, bulk confirm/reject, undo, split/undo; review Y/N/V and undo; rename/reload, merge preview and merge/undo |
| Albums and collections | Selection creates album and opens correct detail; saved filename search creates collection and opens correct media; assistant-off rules-only album |
| Events and Places | Event detail/rename/undo; local map clusters, select cluster and result grid, no external map requests |
| Videos and editing | Native playback/seeking through person moments; keyboard play/pause; rotate/crop/rate/revert with original-byte verification |
| Duplicates/storage | Resolve and undo through keyboard; category preview, selected soft cleanup and undo |
| Libraries | Folder chooser mouse/keyboard, temporary folder add, offline/reconnect, failed/slow share feedback; share connection transport is mocked |
| Settings/health/plugins/diagnostics | Preference persistence, opt-in timings and redacted report, plugin install/permission review/local sandbox/uninstall; integrity job, backup download and restore-confirmation cancellation; backend restore/upgrade tests |
| Layout | Screenshots of Photos, People, Search, Albums, Settings and Review in both themes at 1280 and 390 pixels; axe core at 375 pixels |

Browser tests use the production build and fail on unexpected console exceptions and failed requests. Intentionally injected 503 responses have a separate recovery test; it checks loading, empty state, error and successful Retry without expecting a clean network log. Route rendering alone is not counted as an action test. Backend tests cover formats, detection/tracks, corrections, exclusions, jobs/cancellation/recovery, incremental indexing, search, duplicates, backup/upgrade, privacy, packages, network libraries and authentication.

## Measurements and checks

| Check | Actual result |
| --- | --- |
| Backend default suite, live server stopped | **323 passed**, 2 performance tests deselected, 10 third-party warnings; 169.33 s (after sized-preview regression was added) |
| Performance-marked backend suite | **2 passed**; 100k-item search latency and long-video processing budgets verified |
| Browser workflows/accessibility | **38 distinct workflows passed across final run and targeted retests**; final full run was 35 passed / 3 failed / one Lighthouse skip, then all four targeted retests passed |
| Lighthouse, separate opt-in run | Passed: Photos/Timeline 98 performance; core accessibility and best practices 100 |
| Frontend unit tests | **76 passed**, six files |
| Type check and production build | `npm --prefix frontend run build` passed; large-chunk warning remains |
| Python lint | Ruff passed for backend, tests and scripts |
| Whitespace check | `git diff --check` passed |
| Normal startup/open progress stream shutdown | HTTP 200; graceful shutdown completed in **5.2 s** |

The initial full backend rerun under competing live inference exceeded one VLM wall-clock budget despite passing its correctness checks. The isolated rerun of that test passed, followed by the complete 322-test run, then the updated 323-test run above. Before appropriately sized grid previews, Lighthouse failed its 95-point performance threshold (Photos 91–94 in repeated runs). After the preview change, the separately executed Lighthouse check passed: performance **98** for `/`, `/photos`, `/timeline`, **100** for People/Search; accessibility and best practices **100** on every core screen. Photos LCP fell from 1.9 s to about 1.1 s in the synthetic desktop run, with 0 ms blocking time and no cumulative layout shift. Scores vary with machine workload; these are measured local results, not guaranteed scores for every library.

All 36 browser workflow/accessibility tests in the prior complete run passed; its separate Lighthouse test was the only failure before the preview fix. A newly added fullscreen test found the hidden-toolbar fallback issue, now fixed. The expanded 39-test browser run also includes native keyboard play/pause, indexed-order verification, fullscreen entry/exit and original-export byte comparison. A macOS/Chromium suspension produced `ERR_NETWORK_IO_SUSPENDED` in one route scan and a folder-chooser timeout; those assertions were preserved for rerun. The additional original-download assertion initially assumed a standalone photo link; the application actually provides original ZIP export. The test now executes that visible control and compares its archived file to the original HTTP bytes. The final targeted rerun passed all four checks: light-theme route scan, folder chooser/offline recovery, fullscreen/overlay/metadata/original export, and Recently Added/native video controls. The full run had 35 passed, three failures and one Lighthouse skip; the rerun resolved all three failures. Thus all 38 distinct browser workflows now have passing observed results, plus the separate Lighthouse pass. This is not presented as a single clean 39-test run.

The reproducible disposable face-page benchmark is `PYTHONPATH=. .venv/bin/python scripts/bench_face_pages.py`. Five warm TestClient rounds measured legacy median **61.4 ms / 25 requests / 67,215 bytes** versus bounded median **5.0 ms / one request / 13,603 bytes**. Legacy retrieves 240 faces from 24 sources; the new page retrieves 60. This measures the reduction in work and bounded page size, not identical response volume or real-network latency.

Six first-row synthetic previews totaled **473,940 bytes** as cached JPEGs versus **313,442 bytes** as grid WebP (**33.9% less**). Median generation was **17.23 ms**, versus **0.036 ms** for a warm cache hit. This noisy fixture is deliberately harder to compress than typical photographs; it is not a whole-library benchmark.

On the real library, the largest person had **39,170 faces**. Five read-only HTTP requests returned only 60 faces each, with median **112.3 ms**. No assignments were changed for this measurement.

The default CSS artifact fell from 164.23 kB (31.09 kB gzip) to 156.46 kB (29.81 kB gzip). The main JavaScript artifact remains about 601 kB (180.43 kB gzip); Vite's >500 kB chunk warning remains, including the separately loaded MapLibre chunk. That warning was not suppressed.

## Saved visual evidence

The synthetic fixture screenshots are saved under `artifacts/redesign/`: Photos, People, Search, Albums, Settings and Review in both themes at desktop and phone widths, plus photo shell and person-face views. Photos and People desktop and Photos phone images were inspected visually. They show generated test media, not the user's original photographs.

- [Photos — desktop light](../artifacts/redesign/photos-light-1280.png)
- [Photos — phone dark](../artifacts/redesign/photos-dark-390.png)
- [People — desktop light](../artifacts/redesign/people-light-1280.png)

## Limits

This is broad route and workflow coverage, not certification that every possible setting value, permission combination, codec, dialog action and failure mode has been executed. In particular, live original purge/move, real SMB/NFS mount credentials and permission loss, backup restore of the user's database, and browser conversion of every unsupported video codec were not exercised against the user's library. No destructive live operation was used to test a button. Optional local VLM behavior is tested as unavailable/rules-only in the browser fixture; backend real face-model tests ran, but no user-triggered full reindex was initiated. GPU-specific behavior was not verified.

Existing third-party Python deprecation warnings and large JS chunks remain. Older already-open pages may reference assets removed by a new Vite build; refresh loads the current build. Full cold Settings storage usage can still take seconds for hundreds of thousands of generated thumbnail files, although it no longer blocks startup preferences. Storage numbers remain cached for up to five minutes as before.

## Start

From the project root:

```sh
npm --prefix frontend run build
LFS_HOST=127.0.0.1 .venv/bin/python -m backend
```

The updated server was restarted using the normal entry point. Live health, populated timeline and a sized preview returned HTTP 200; startup preferences returned in 2.4 ms. SQLite quick_check and all four record counts remained unchanged. Photos was opened successfully in a fresh in-app browser tab; its new console error log was empty.

The build is already generated. Open `http://127.0.0.1:8765/`. The server serves `frontend/dist`; Vite's dev server is optional. Existing data/model locations and library registrations are preserved.
