# Liquid Glass presentation audit — 11 October 2026

The application now uses a shared iOS-inspired glass material in `frontend/src/liquid-glass.css`, loaded after the existing responsive layout theme. This is an original web implementation, not an assertion that it reproduces an official iOS 27 specification.

## Coverage

- Desktop sidebar, mobile header, bottom navigation, expanded navigation sheet, search bar and command dialog.
- Shared buttons, icon controls, fields, selects, text areas, checkboxes, sliders, badges, keyboard hints, segmented controls, tabs and focus states.
- Dashboard, People, clusters, albums, events, review, duplicates, cleanup, libraries, settings, health, storage, plugins and diagnostics panels.
- Photo timeline, thumbnail corners, selection state, photo/video viewer chrome, metadata panels, dialogs, confirmations, empty/error states, notifications and map controls.
- Light and dark colour tokens, system typography, subtle specular edges and translucent surfaces. Original image pixels and canvas/video content remain unfiltered.

Large collections do not create a backdrop-blur layer for each thumbnail or library-grid card. Blur is restricted to floating navigation, modal chrome and large detail/settings panels. Reduced transparency, reduced motion, unsupported backdrop filters and forced colours have CSS fallbacks. Mobile review actions retain a 44px minimum touch height; navigation sheets retain their full viewport width and two-column tool layout.

The subsequent “pure liquid glass” refinement increases surface transparency, backdrop saturation and blur, adds brighter specular edges and translucent control highlights, and strengthens the fixed ambient background in both themes. Thumbnail pixels remain unfiltered.
Its all-screen light/dark accessibility, phone accessibility, keyboard focus, and four responsive nested-view audits passed all eight selected Playwright tests. Live Settings screenshots at desktop and phone widths in light/dark showed no horizontal overflow. The reduced-transparency cascade was corrected for dark mode as well.

The application-wide transparent refinement also removes opaque fills from primary/danger buttons, inputs, selected tabs, status badges, review subpanels and viewer chrome. Shared soft/hover/selection colours are now alpha tints in both themes. The floating mobile capsule uses the shared clear glass tint, with theme-aware text instead of a dark filled background. Native select option menus remain solid for readable operating-system rendering; the media stage keeps a neutral background for accurate photo/video viewing.
This refinement passed the build and ten selected browser tests: all-screen light/dark accessibility, phone accessibility, keyboard focus, four responsive nested-view audits and two photo/video viewer workflows. Live light/dark Settings views at 390px/1440px were also inspected, with no horizontal overflow.

## Verification

The live production application was started with `LFS_HOST=127.0.0.1 NO_ALBUMENTATIONS_UPDATE=1 .venv/bin/python -m backend`.

A read-only live audit visited 24 top-level routes plus a populated person and event detail, at 1440×900, 820×1180, 390×844 and 320×740: 104 views, no recorded page errors, HTTP 5xx responses, or horizontal layout violations. Fully loaded Photos, mobile Review and dark Settings screenshots were inspected separately.

`npm run build` passed TypeScript and Vite compilation, with the existing large-chunk advisory. `npm test` passed 76 tests in six files. The complete existing Playwright suite passed 42 tests; one opt-in Lighthouse test was skipped. This includes all-screen light/dark WCAG checks, phone accessibility, keyboard focus, four responsive nested-view audits, face decisions/undo, person rename/merge, search/favorites, photo/video viewers, album/event actions, library connection/offline recovery, storage, plugins, settings and export/import workflows. These tests use isolated fixtures rather than changing production identities or originals. `git diff --check` also passed.

## Limits

Browser verification uses Chromium on this Mac, not physical iPhones/iPads or Safari. The opt-in Lighthouse test is not enabled in the normal suite; no Lighthouse performance score is claimed. No benchmark claim is made for compositor/GPU costs of the glass effect. Third-party plugin iframe contents and native browser controls keep their own rendering; the application-owned host panels and controls use the shared design. Native operating-system picker UI is outside the web theme.

No dependency, API, schema, embedding or recognition model changes were required for this presentation update. Original media are not modified by the theme.
