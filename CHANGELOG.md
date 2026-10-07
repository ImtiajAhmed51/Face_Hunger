# Changelog

All notable changes. Dates are when the work landed on `main`.

## 1.0.0 - 2026-10-08

### Added
- **Edit tools**: non-destructive crop, rotate, flip, ratings, colour labels and flags; XMP and JSON
  sidecars; per-item history and revert; batch edits; export with edits applied.
- **Share safely**: blur, pixelate or mask chosen faces, strip metadata, verify the output, export
  as new files only.
- **Encrypted packages** (`.fhpack`): AES-256-GCM with a scrypt-derived key; export or import a
  library, a person or an album with integrity checks and conflict handling.
- **Local assistant (optional, off by default)**: SmolVLM2-500M for album titles, captions and
  search expansions, with a rule-based fallback; never loaded unless enabled.
- **Storage screen**: large files, extra copies, old low-quality video, screenshots, blurry and
  dark photos; savings estimate; previewed, undoable cleanup.
- **Re-index with another model**: build a new index beside the current one, compare, switch,
  roll back; resumable after a crash.
- **Plugins**: `plugin.toml` manifest, versioned plugin API (1.0), sandboxed subprocess, explicit
  permissions, plugin manager screen; extension points for embedding models, classifiers, search
  signals, export targets and UI panels; two reference plugins.
- **Diagnostics (optional, off by default)**: local timing traces, a Diagnostics screen, and a
  redacted report you export by hand.
- **App lock (optional)**: a password for the whole app (Settings or `LFS_APP_PASSWORD`).
- `GET /api/health/live` for container health checks.
- Dockerfile, pinned `requirements.lock`, release checklist, and architecture, operations,
  extension, security, privacy, licence and accessibility documents.

### Security
- CSRF header enforced in middleware for every state-changing API route (previously checked in
  each handler after body validation).
- State-changing requests from a foreign `Origin` are refused; requests with an unknown `Host`
  name are refused (DNS rebinding).
- Fixed: the single-page-app fallback could serve files outside the frontend folder through
  encoded `..` segments.
- Backup restore accepts only the files a backup can contain, checks declared sizes and
  compression ratios, and checks free space before extracting.
- Content-Security-Policy on the app page; `nosniff`, `Referrer-Policy`, frame and resource policies
  on every response.

### Changed
- Search can use plugin labels and plugin signals (`label`, `plugin_<id>` in the response).
- The active model for a role can be pinned (`settings.active_models`); plugin models are used only
  when pinned.
- Accessibility: all screens pass axe WCAG 2.2 AA in light, dark and phone layouts; larger event
  checkboxes; no horizontal scrolling on the home screen at phone width.

### Migrations
- 14 `edits` (`media_edits`, `edit_history`), 15 `captions` (`media_captions`, `caption_fts`),
  16 `plugins` (`plugins`, `plugin_labels`, `plugin_media_done`). Each runs once, after an automatic
  database backup.

### Upgrade notes
- A reverse proxy or custom host name now needs `LFS_ALLOWED_HOSTS=<name>`.
- API clients must send `X-LFS-Request: 1` on every write.

## 0.2.0 (Phase 2)
RAW/HEIC/AVIF, capture metadata with timeline and offline map, quality scores and best shots,
events, video face tracks and moments, albums/favourites/collections with undo and audit log,
keyboard-first duplicate resolver, code-split frontend with i18n (English, Bangla) and browser tests.
Migrations 9-13.

## 0.1.0 (Phase 1)
App factory with routers and services, versioned multi-model vector stores, hybrid search with
saved searches, priority job queue with SSE progress and file watcher, design-system frontend with
virtualised grids, structured logging, health checks, integrity checks and backup/restore.
Migrations 6-8.
