# Visual QA report (partial)

Status: **in progress, stopped early.** This pass reviewed real screenshots of the running app on a
photo-like fixture library and fixed what it found. It did not complete the full matrix in the brief.

## What was captured and looked at

- Harness: `frontend/playwright.visual.config.ts`, `frontend/visual/` (capture of every screen and of
  interactive/empty/error/loading states), `scripts/make_visual_fixture.py` (fixture with mixed aspect
  ratios, long and Bangla names, albums, favourites, deleted items), `scripts/visual_sheet.py` (contact sheets).
  Output goes to `artifacts/visual/` (not committed; regenerate with the commands below).
- **Looked at by eye:** all 24 screens, populated state, English, at 390x844 and 1280x800 in light and dark
  (contact sheets), plus 9 screens at 1920x1080; phone viewer, error state and command palette.
- **Not looked at:** high-contrast, Bangla, 150%/200% zoom, the other five viewports, Firefox and WebKit,
  most interactive states (dialogs, menus, toast, selection, hover, focus), empty first-run screens,
  motion recordings. No visual-regression baselines were committed and no `design-audit.html` was produced.

## Defects found and fixed

| Defect | Where | Severity | Fix |
| --- | --- | --- | --- |
| Text below 12px (7-11px) in ~100 rules | badges, captions, eyebrows, nav labels, metadata | major | type scale tokens; nothing below 12px |
| 52 distinct font sizes and 43 distinct radii, almost all hard-coded | all stylesheets | major | 272 font sizes, 16 fluid sizes and 174 radii replaced with `--text-*` / `--radius-*` tokens (11 type steps, 6 radii) |
| People cards: no gap under the filter bar; image height changed with name length | People | major | spacing; name clamped to two lines with reserved height |
| Event cards: cover height changed when people chips were present | Events | major | fixed caption height; chips on one line |
| Error and "empty library" messages shown together, with "0 results" | Photos (and other grids) on a failed request | major | empty state and count are hidden while an error is shown |
| Settings > Local storage showed "Unknown" for every size | Settings | major | API now returns sizes (`storage_bytes`) |
| Long path in a warning ran outside its box | Health, phone | major | wraps anywhere |
| Active sidebar item hidden below the fold on short windows | sidebar, 800px tall | major | scrolled into view on navigation |
| Photo grid was one column on phones (skeleton showed two) | Photos, 390px | major | never fewer than two columns |
| Person header buttons in a ragged right-aligned stack | Person | minor | one left-aligned row |
| Album form: button lower than its input | Albums | minor | aligned |
| Same icon for different destinations (Albums/Map, Clusters/Events/Plugins, Timeline/Diagnostics; Favorite/Find similar) | sidebar, viewer | minor | distinct icons |
| "1 items", "1 results" | Storage, grids | cosmetic | singular forms |
| Keyboard-shortcut tip shown on touch devices | grids, phone | cosmetic | hidden without hover |
| Status rows in lower case ("database", "disk") | Health | cosmetic | capitalised |
| Storage tiles uneven when a title wrapped; Plugins install form outside any panel | Storage, Plugins | cosmetic | fixed |

After the fixes the affected screens were recaptured and checked again at 390x844 dark and 1280x800 light.
The error-state fix, the favourite icon and the Albums and Events changes were **not** re-screenshotted
before this pass stopped; they are covered only by the type check, unit tests and browser tests.

## Known remaining issues (not fixed)

1. Content has no maximum width: at 1920px and wider, toolbars and text lines stretch across the screen.
2. Map without a base map is an empty grid with dots, and the canvas is narrower than the page.
3. Cluster cards have ragged heights and empty grey areas.
4. Cleanup: nine stat tiles leave an orphan row; one tile shows an arrow instead of a number.
5. Search placeholder is cut off at phone width.
6. Viewer toolbar on phones leaves the fullscreen button alone on its own row.
7. Settings shows a raw lower-case "error" badge and a long file path in the alert.
8. Contrast was only checked by the existing axe tests (WCAG AA, light and dark), not re-measured here.

## Verdict

**NOT READY to call ship-ready on looks.** No blocker was seen on the screens reviewed, but two thirds of
the requested matrix was not inspected, and items 1-3 above are visible to any user on a large monitor.

## Re-run

```bash
cd frontend && npm run build
VP=390x844,1280x800 THEMES=light,dark LANGS=en LFS_PYTHON=$(which python) \
  npx playwright test -c playwright.visual.config.ts --project=chromium capture
LFS_PYTHON=$(which python) npx playwright test -c playwright.visual.config.ts --project=chromium states
python scripts/visual_sheet.py photos        # contact sheet in artifacts/visual/_sheets
```
