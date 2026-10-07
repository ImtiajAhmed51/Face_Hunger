# Operations guide

## Install and run

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e .                      # or, for the tested versions: pip install --no-deps -r requirements.lock && pip install --no-deps -e .
(cd frontend && npm ci && npm run build)
python scripts/fetch_models.py        # optional, one time: SigLIP 2 + DINOv2 (Apache-2.0)
python -m backend                     # http://127.0.0.1:8765
```

Face models (`buffalo_l`) are not downloaded for you: their licence is non-commercial research
only. Put them in `LFS_MODEL_DIR/buffalo_l` if that fits your use.

Container (not built or run in this release's test environment; see docs/RELEASE.md):

```bash
docker build --build-arg SOURCE_DATE_EPOCH=$(git log -1 --format=%ct) -t face-hunger:1.0.0 .
docker run --rm -p 127.0.0.1:8765:8765 -v fh-data:/data -v /path/to/models:/models:ro \
  -v /path/to/photos:/photos:ro face-hunger:1.0.0
```

## Configuration (environment, prefix `LFS_`, or `.env`)

| Variable | Default | Meaning |
| --- | --- | --- |
| `LFS_DATA_DIR` | `data` | index, vectors, thumbnails, backups |
| `LFS_MODEL_DIR` | `models` | the only place models are loaded from |
| `LFS_HOST` / `LFS_PORT` | `0.0.0.0` / `8765` | set host to `127.0.0.1` to stay on this machine |
| `LFS_ALLOWED_ROOTS` | home folder | `;`-separated folders libraries may live in |
| `LFS_ALLOWED_HOSTS` | (none) | extra host names the app answers to (IP addresses and `localhost` always work) |
| `LFS_APP_PASSWORD` | (none) | turns the app lock on from the environment |
| `LFS_WATCH` | `true` | watch library folders for new files |
| `LFS_MODEL_IDLE_SECONDS` | `300` | unload optional models after this idle time |
| `LFS_DINO_VARIANT` | `small` | `small` or `base` |
| `LFS_LOG_LEVEL` / `LFS_LOG_FORMAT` | `INFO` / `json` | logs go to stderr, one JSON object per line with a `request_id` |

After upgrading from a version before 1.0, note two behaviour changes: requests whose Host header
is a DNS name other than `localhost` need that name in `LFS_ALLOWED_HOSTS` (for example behind a
reverse proxy), and scripts that call the API must send `X-LFS-Request: 1` on every write (this was
already required by the handlers; it is now enforced before the body is read).

## Health

- `GET /api/health/live`: liveness only, open even when the app is locked (container health check).
- `GET /api/health`, `GET /api/health/library`, and the Health screen: storage, model status,
  configuration warnings, integrity check (`POST /api/health/check`).
- Jobs: the Jobs panel, `GET /api/jobs`, live progress at `GET /api/jobs/events` (SSE).

## Upgrades and migrations

Start the new version on the old data folder. Pending migrations run at startup; before the first
one, the database is copied to `data/backups/index-pre-v<N>-<time>.sqlite`. Migrations are
idempotent. To go back: stop the app, restore that copy over `index.sqlite`, start the old version.
Vector stores are per model and are never rewritten by an upgrade.

## Backup and restore

- Full index backup: Health screen, `POST /api/backup/export`, or `python -m backend.ops.backup export`.
  Output: `data/exports/face-hunger-backup-*.zip` (database snapshot, vectors, optionally thumbnails).
  Originals are not included: back those up with your normal tool.
- Restore: Health screen or `POST /api/backup/restore`. The archive is verified (checksums, expected
  file names, sizes), staged, and swapped in at the next start; the previous data moves to
  `data/backups/pre-restore-*`.
- Encrypted packages (`.fhpack`) move a library, a person or an album between machines.

## Routine tasks

| Task | How |
| --- | --- |
| Free disk space | Storage screen (previews, estimate, undo); empty `data/duplicate-bin` only after you are sure |
| Change the embedding model | Health > "Re-index with another model": builds beside the current index, compare, switch, roll back |
| Rebuild a damaged index | Health > integrity check; `rebuild_index` job per model |
| Reset derived data | stop the app; delete `thumbnails/`, `previews/`, `video_cache/` |
| Forgotten app password | stop the app; `sqlite3 data/index.sqlite "DELETE FROM settings WHERE key='app_lock'"` (needs file access, which is the point) |
| A plugin misbehaves | Plugins screen: disable or uninstall; its log is `data/plugins/<id>.log` |
| Find what is slow | Diagnostics screen: turn on, use the app, read or export the report, turn off |

## Sizing (measured, see docs/PERFORMANCE.md)

Target: 16 GB laptop, 100k-500k items. Memory is dominated by the in-RAM ANN indexes (roughly
1.2 kB per item per 384-dimension model) and by whichever models are loaded (unloaded when idle).

## Checks before a release

```bash
ruff check backend tests scripts plugins && python -m pytest -q
python -m pytest -m perf                       # benchmarks (slow)
cd frontend && npx tsc -b && npx vitest run && npx vite build
npx playwright install chromium --only-shell   # once
LFS_PYTHON=$(which python) npx playwright test
LIGHTHOUSE=1 LFS_PYTHON=$(which python) npx playwright test e2e/lighthouse.spec.ts
```
